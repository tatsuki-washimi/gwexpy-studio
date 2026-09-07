"""Create and verify detached, canonical release-source manifests.

The manifest deliberately identifies source content rather than a Git commit.
This lets a private release candidate and a public checkout prove that their
exported source trees are identical without requiring their Git histories to
be shared.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from functools import cache
from pathlib import Path, PurePosixPath
from typing import Literal, cast

ManifestAction = Literal["include", "exclude", "deny"]
FileType = Literal["file", "directory"]
_CHECKOUT_METADATA_ROOTS = frozenset({".git"})


class ReleaseSourceError(RuntimeError):
    """Base error for release-source identity failures."""


class PolicySyntaxError(ReleaseSourceError, ValueError):
    """Raised when an allowlist cannot safely describe a relative path."""


class UnclassifiedPathError(ReleaseSourceError):
    """Raised when a path has no explicit allowlist classification."""


class ExcludedPathError(ReleaseSourceError):
    """Raised when an omitted path appears in a purported release tree."""


class DeniedPathError(ReleaseSourceError):
    """Raised when a private path appears in a purported release tree."""


class SymlinkNotAllowed(ReleaseSourceError):
    """Raised because release source snapshots must not contain symlinks."""


class UnexpectedFileTypeError(ReleaseSourceError):
    """Raised for devices, sockets, FIFOs, or other non-portable entries."""


class UnsafePermissionError(ReleaseSourceError):
    """Raised for set-ID or sticky permissions in portable release source."""


class ManifestFormatError(ReleaseSourceError, ValueError):
    """Raised when a detached manifest is malformed or non-canonical."""


@dataclass(frozen=True)
class PolicyRule:
    """One ordered rule from the release-source allowlist."""

    action: ManifestAction
    pattern: str
    line_number: int


@dataclass(frozen=True)
class ReleaseSourcePolicy:
    """Ordered include, exclude, and hard-deny rules for release source."""

    rules: tuple[PolicyRule, ...]

    def classify(self, relative_path: str) -> ManifestAction | None:
        """Return the policy action for a normalized relative path.

        ``deny`` is deliberately absolute: a later include cannot make a
        private path part of a public release source.
        """
        _validate_relative_path(relative_path)
        action: ManifestAction | None = None
        for rule in self.rules:
            if not _glob_matches(relative_path, rule.pattern):
                continue
            if rule.action == "deny":
                return "deny"
            action = rule.action
        return action


@dataclass(frozen=True)
class ManifestEntry:
    """One portable source-tree entry in a canonical manifest."""

    path: str
    file_type: FileType
    mode: str
    sha256: str
    symlink_target: str = ""

    def as_dict(self) -> dict[str, str]:
        """Return the stable wire representation used by ``to_bytes``."""
        return {
            "path": self.path,
            "file_type": self.file_type,
            "mode": self.mode,
            "sha256": self.sha256,
            "symlink_target": self.symlink_target,
        }


@dataclass(frozen=True)
class ReleaseSourceManifest:
    """A detached, mtime-free description of a release-source tree."""

    entries: tuple[ManifestEntry, ...]

    def to_bytes(self) -> bytes:
        """Serialize this manifest with a stable JSON representation."""
        payload = {
            "entries": [entry.as_dict() for entry in self.entries],
            "schema": 1,
        }
        return (
            json.dumps(
                payload,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\n"
        )


@dataclass(frozen=True)
class ManifestDifference:
    """One useful difference between expected and actual source manifests."""

    kind: Literal["missing", "unexpected", "changed"]
    path: str
    detail: str


class ManifestMismatch(ReleaseSourceError):
    """Raised when a fresh source tree differs from the detached sidecar."""

    def __init__(self, differences: Iterable[ManifestDifference]) -> None:
        """Describe every relevant difference in deterministic path order."""
        self.differences = tuple(differences)
        lines = ["release-source manifest mismatch:"]
        lines.extend(
            f"- {difference.kind}: {difference.path} ({difference.detail})"
            for difference in self.differences
        )
        super().__init__("\n".join(lines))


@dataclass(frozen=True)
class _TreeNode:
    """An inspected filesystem node, pinned by its device and inode."""

    relative_path: str
    file_type: FileType | Literal["symlink", "other"]
    mode: int
    device: int
    inode: int
    size: int
    mtime_ns: int
    ctime_ns: int


def load_policy(path: Path) -> ReleaseSourcePolicy:
    """Read a line-oriented release-source allowlist from ``path``.

    Each non-comment line is exactly ``include PATTERN``, ``exclude PATTERN``,
    or ``deny PATTERN``. Rules are ordered; the final include/exclude match
    wins, while any matching deny always wins.
    """
    return _parse_policy_text(path.read_text(encoding="utf-8"), path)


def load_policy_bytes(content: bytes, path: Path) -> ReleaseSourcePolicy:
    """Parse one already-captured UTF-8 allowlist without rereading its path."""
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PolicySyntaxError(f"{path}: policy must be valid UTF-8") from exc
    return _parse_policy_text(text, path)


def _parse_policy_text(text: str, path: Path) -> ReleaseSourcePolicy:
    """Apply the common policy grammar to a stable text snapshot."""
    rules: list[PolicyRule] = []
    for line_number, raw_line in enumerate(text.splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) != 2 or parts[0] not in {"include", "exclude", "deny"}:
            raise PolicySyntaxError(
                f"{path}:{line_number}: expected "
                "'include|exclude|deny RELATIVE_PATTERN'"
            )
        action = cast(ManifestAction, parts[0])
        pattern = parts[1]
        _validate_relative_pattern(pattern, path, line_number)
        rules.append(PolicyRule(action, pattern, line_number))
    if not rules:
        raise PolicySyntaxError(f"{path}: policy must contain at least one rule")
    return ReleaseSourcePolicy(tuple(rules))


def build_manifest(root: Path, policy: ReleaseSourcePolicy) -> ReleaseSourceManifest:
    """Build a canonical manifest for an already-public release tree.

    This is intentionally strict: excluded, denied, unclassified, symlinked,
    and non-portable filesystem entries all make the purported public tree
    invalid. ``export_release_source`` is the operation that removes explicitly
    excluded private input before this function is called on the snapshot.
    """
    with _open_root_directory(root) as root_descriptor:
        _validate_root_mode(os.fstat(root_descriptor), root)
        return _build_manifest_from_root_descriptor(root_descriptor, policy)


def build_public_checkout_manifest(
    root: Path,
    policy: ReleaseSourcePolicy,
) -> ReleaseSourceManifest:
    """Build the canonical manifest for a clean public Git checkout.

    ``.git`` is local checkout metadata rather than public source.  It is the
    sole omitted root: all other files must satisfy the public policy exactly.
    """
    with _open_root_directory(root) as root_descriptor:
        _validate_root_mode(os.fstat(root_descriptor), root)
        return _build_manifest_from_root_descriptor(
            root_descriptor,
            policy,
            ignored_root_names=_CHECKOUT_METADATA_ROOTS,
        )


def _build_manifest_from_root_descriptor(
    root_descriptor: int,
    policy: ReleaseSourcePolicy,
    *,
    ignored_root_names: frozenset[str] = frozenset(),
) -> ReleaseSourceManifest:
    """Build a manifest while retaining the opened root directory identity."""
    nodes = tuple(
        _walk_tree_from_root_descriptor(
            root_descriptor,
            ignored_root_names=ignored_root_names,
        )
    )
    _validate_public_tree(nodes, policy)
    entries = tuple(
        sorted(
            (_manifest_entry(node, root_descriptor) for node in nodes),
            key=lambda entry: entry.path.encode("utf-8"),
        )
    )
    return ReleaseSourceManifest(entries)


def _manifest_entry(node: _TreeNode, root_descriptor: int) -> ManifestEntry:
    """Convert a prevalidated regular file or directory into a manifest entry."""
    if node.file_type not in {"file", "directory"}:
        raise AssertionError("release tree validation must reject non-manifest nodes")
    return ManifestEntry(
        path=node.relative_path,
        file_type=cast(FileType, node.file_type),
        mode=f"{canonical_source_mode(cast(FileType, node.file_type), node.mode):04o}",
        sha256=(
            _sha256_regular_node(root_descriptor, node)
            if node.file_type == "file"
            else ""
        ),
    )


def canonical_source_mode(file_type: FileType, source_mode: int) -> int:
    """Map host permissions to the portable modes Git can represent.

    Git records whether a regular file is executable, but not directory modes
    or arbitrary owner/group permission differences.  Canonical manifests and
    exported snapshots therefore use ``0755`` for directories and executable
    files, and ``0644`` for all other regular files.
    """
    if file_type == "directory" or source_mode & 0o111:
        return 0o755
    return 0o644


def manifest_digest(manifest: ReleaseSourceManifest) -> str:
    """Return the SHA-256 of the detached canonical manifest sidecar."""
    return hashlib.sha256(manifest.to_bytes()).hexdigest()


def write_manifest(manifest: ReleaseSourceManifest, path: Path) -> None:
    """Write a detached manifest once; never overwrite existing evidence."""
    if path.exists() or path.is_symlink():
        raise FileExistsError(f"refusing to overwrite detached manifest: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    with _open_root_directory(path.parent) as parent_descriptor:
        _write_manifest_at(manifest, parent_descriptor, path.name, path)


def _write_manifest_at(
    manifest: ReleaseSourceManifest,
    parent_descriptor: int,
    name: str,
    display_path: Path,
) -> os.stat_result:
    """Create one manifest leaf through an already-retained parent directory."""
    if not name or name in {".", ".."} or "/" in name:
        raise ValueError("detached manifest name must be a single path component")
    try:
        os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    except FileNotFoundError:
        pass
    else:
        raise FileExistsError(
            f"refusing to overwrite detached manifest: {display_path}"
        )
    flags = (
        os.O_RDWR
        | os.O_CREAT
        | os.O_EXCL
        | _no_follow_flag()
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        descriptor = os.open(name, flags, 0o644, dir_fd=parent_descriptor)
    except FileExistsError as exc:
        raise FileExistsError(
            f"refusing to overwrite detached manifest: {display_path}"
        ) from exc
    try:
        payload = manifest.to_bytes()
        _write_all(descriptor, payload)
        os.fchmod(descriptor, 0o644)
        expected_status = os.fstat(descriptor)
        os.lseek(descriptor, 0, os.SEEK_SET)
        actual = bytearray()
        while chunk := os.read(descriptor, 1024 * 1024):
            actual.extend(chunk)
        if bytes(actual) != payload:
            raise ReleaseSourceError(
                f"detached manifest changed while writing: {display_path}"
            )
        _assert_status_unchanged(
            expected_status,
            os.fstat(descriptor),
            display_path.as_posix(),
        )
        os.fsync(descriptor)
        _assert_status_unchanged(
            expected_status,
            os.fstat(descriptor),
            display_path.as_posix(),
        )
        return expected_status
    finally:
        os.close(descriptor)


def read_manifest(path: Path) -> ReleaseSourceManifest:
    """Read one detached manifest through a no-follow regular-file descriptor."""
    with _open_manifest_file(path) as (descriptor, _parent_descriptor, witness):
        raw = _read_stable_manifest_bytes(descriptor, witness, path)
    return _parse_manifest_bytes(raw, path)


def _parse_manifest_bytes(raw: bytes, path: Path) -> ReleaseSourceManifest:
    """Parse and require the one canonical JSON wire form for a sidecar."""
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ManifestFormatError(f"invalid manifest JSON: {path}") from exc
    if (
        not isinstance(payload, dict)
        or type(payload.get("schema")) is not int
        or payload["schema"] != 1
    ):
        raise ManifestFormatError(f"unsupported manifest schema: {path}")
    raw_entries = payload.get("entries")
    if not isinstance(raw_entries, list):
        raise ManifestFormatError(f"manifest entries must be a list: {path}")

    entries: list[ManifestEntry] = []
    for index, raw_entry in enumerate(raw_entries):
        if not isinstance(raw_entry, dict):
            raise ManifestFormatError(f"manifest entry {index} is not an object")
        try:
            for field in (
                "path",
                "file_type",
                "mode",
                "sha256",
                "symlink_target",
            ):
                if not isinstance(raw_entry[field], str):
                    raise ManifestFormatError(
                        f"manifest entry {index} {field} must be a string"
                    )
            entry = ManifestEntry(
                path=cast(str, raw_entry["path"]),
                file_type=cast(FileType, raw_entry["file_type"]),
                mode=cast(str, raw_entry["mode"]),
                sha256=cast(str, raw_entry["sha256"]),
                symlink_target=cast(str, raw_entry["symlink_target"]),
            )
        except KeyError as exc:
            raise ManifestFormatError(f"manifest entry {index} is incomplete") from exc
        _validate_manifest_entry(entry, index)
        entries.append(entry)

    manifest = ReleaseSourceManifest(tuple(entries))
    paths = [entry.path for entry in manifest.entries]
    if len(paths) != len(set(paths)):
        raise ManifestFormatError(f"manifest has duplicate paths: {path}")
    canonical_entries = tuple(
        sorted(manifest.entries, key=lambda entry: entry.path.encode("utf-8"))
    )
    if manifest.entries != canonical_entries or manifest.to_bytes() != raw:
        raise ManifestFormatError(f"manifest is not canonical: {path}")
    return manifest


def compare_manifests(
    expected: ReleaseSourceManifest,
    actual: ReleaseSourceManifest,
) -> tuple[ManifestDifference, ...]:
    """Return sorted, field-level differences useful in release diagnostics."""
    expected_by_path = {entry.path: entry for entry in expected.entries}
    actual_by_path = {entry.path: entry for entry in actual.entries}
    differences: list[ManifestDifference] = []
    for path in sorted(set(expected_by_path) | set(actual_by_path), key=str.encode):
        expected_entry = expected_by_path.get(path)
        actual_entry = actual_by_path.get(path)
        if expected_entry is None:
            differences.append(
                ManifestDifference("unexpected", path, "not in expected manifest")
            )
            continue
        if actual_entry is None:
            differences.append(
                ManifestDifference("missing", path, "missing from source tree")
            )
            continue
        if expected_entry == actual_entry:
            continue
        changed_fields = [
            field
            for field in ("file_type", "mode", "sha256", "symlink_target")
            if getattr(expected_entry, field) != getattr(actual_entry, field)
        ]
        differences.append(
            ManifestDifference(
                "changed",
                path,
                "changed fields: " + ", ".join(changed_fields),
            )
        )
    return tuple(differences)


def verify_manifest(
    root: Path,
    policy: ReleaseSourcePolicy,
    expected: ReleaseSourceManifest | Path,
) -> ReleaseSourceManifest:
    """Verify a public checkout against a detached expected manifest."""
    return _verify_manifest(root, policy, expected, build_manifest)


def verify_public_checkout_manifest(
    root: Path,
    policy: ReleaseSourcePolicy,
    expected: ReleaseSourceManifest | Path,
) -> ReleaseSourceManifest:
    """Verify ``M(P)`` directly while omitting only local ``.git`` metadata."""
    return _verify_manifest(root, policy, expected, build_public_checkout_manifest)


def _verify_manifest(
    root: Path,
    policy: ReleaseSourcePolicy,
    expected: ReleaseSourceManifest | Path,
    manifest_builder: Callable[[Path, ReleaseSourcePolicy], ReleaseSourceManifest],
) -> ReleaseSourceManifest:
    """Compare one safely read sidecar with a manifest built from ``root``."""
    if isinstance(expected, Path):
        with _open_manifest_file(expected) as (
            descriptor,
            parent_descriptor,
            witness,
        ):
            raw = _read_stable_manifest_bytes(descriptor, witness, expected)
            expected_manifest = _parse_manifest_bytes(raw, expected)
            actual = manifest_builder(root, policy)
            _assert_manifest_descriptor_unchanged(descriptor, witness, expected)
            _assert_manifest_path_unchanged(
                expected,
                parent_descriptor,
                witness,
            )
    else:
        expected_manifest = expected
        actual = manifest_builder(root, policy)
    differences = compare_manifests(expected_manifest, actual)
    if differences:
        raise ManifestMismatch(differences)
    return actual


@contextmanager
def _open_manifest_file(path: Path) -> Iterator[tuple[int, int, os.stat_result]]:
    """Retain a sidecar parent and no-follow regular-file descriptor."""
    absolute_path = Path(os.path.abspath(path))
    if not absolute_path.name or absolute_path.name in {".", ".."}:
        raise ManifestFormatError(f"invalid detached manifest path: {path}")
    with _open_root_directory(absolute_path.parent) as parent_descriptor:
        try:
            descriptor = os.open(
                absolute_path.name,
                os.O_RDONLY | _no_follow_flag() | getattr(os, "O_CLOEXEC", 0),
                dir_fd=parent_descriptor,
            )
        except OSError as exc:
            raise ManifestFormatError(
                f"cannot safely open detached manifest: {path}"
            ) from exc
        try:
            witness = os.fstat(descriptor)
            if not stat.S_ISREG(witness.st_mode):
                raise ManifestFormatError(
                    f"detached manifest is not a regular file: {path}"
                )
            if stat.S_IMODE(witness.st_mode) & 0o7000:
                raise ManifestFormatError(
                    f"detached manifest has unsafe special permissions: {path}"
                )
            yield descriptor, parent_descriptor, witness
        finally:
            os.close(descriptor)


def _read_stable_manifest_bytes(
    descriptor: int,
    witness: os.stat_result,
    path: Path,
) -> bytes:
    """Read one retained sidecar and reject writes that race the read."""
    raw = bytearray()
    while chunk := os.read(descriptor, 1024 * 1024):
        raw.extend(chunk)
    _assert_manifest_descriptor_unchanged(descriptor, witness, path)
    return bytes(raw)


def _assert_manifest_descriptor_unchanged(
    descriptor: int,
    witness: os.stat_result,
    path: Path,
) -> None:
    """Reject a write or replacement of an evidence descriptor."""
    if not _same_status_witness(witness, os.fstat(descriptor)):
        raise ReleaseSourceError(
            f"detached manifest changed during verification: {path}"
        )


def _assert_manifest_path_unchanged(
    path: Path,
    parent_descriptor: int,
    witness: os.stat_result,
) -> None:
    """Reject a sidecar or parent replacement after source verification."""
    absolute_path = Path(os.path.abspath(path))
    with _open_root_directory(absolute_path.parent) as current_parent:
        if not _same_node_identity(
            os.fstat(parent_descriptor),
            os.fstat(current_parent),
        ):
            raise ReleaseSourceError(
                f"detached manifest parent changed during verification: {path}"
            )
    try:
        current = os.stat(
            absolute_path.name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
    except OSError as exc:
        raise ReleaseSourceError(
            f"detached manifest disappeared during verification: {path}"
        ) from exc
    if not stat.S_ISREG(current.st_mode) or not _same_status_witness(current, witness):
        raise ReleaseSourceError(
            f"detached manifest changed during verification: {path}"
        )


def _validate_public_tree(
    nodes: tuple[_TreeNode, ...], policy: ReleaseSourcePolicy
) -> None:
    for node in nodes:
        _validate_safe_mode(node)
        if node.file_type == "symlink":
            raise SymlinkNotAllowed(
                f"symlinks are not permitted in release source: {node.relative_path}"
            )
        if node.file_type == "other":
            raise UnexpectedFileTypeError(
                f"unsupported file type in release source: {node.relative_path}"
            )

    included_paths = {
        node.relative_path
        for node in nodes
        if policy.classify(node.relative_path) == "include"
    }
    for node in nodes:
        decision = policy.classify(node.relative_path)
        if decision == "deny":
            raise DeniedPathError(
                f"denied private path in release source: {node.relative_path}"
            )
        if decision == "include":
            continue
        is_container = node.file_type == "directory" and any(
            path.startswith(f"{node.relative_path}/") for path in included_paths
        )
        if is_container:
            continue
        if decision == "exclude":
            raise ExcludedPathError(
                f"excluded path in release source: {node.relative_path}"
            )
        raise UnclassifiedPathError(
            f"unclassified path in release source: {node.relative_path}"
        )


def _walk_tree(root: Path) -> Iterable[_TreeNode]:
    """Walk a tree from a retained root fd without following child symlinks."""
    with _open_root_directory(root) as root_descriptor:
        yield from _walk_tree_from_root_descriptor(root_descriptor)


@contextmanager
def _open_root_directory(root: Path) -> Iterator[int]:
    """Open an existing release root once and retain its directory identity."""
    try:
        root_status = os.lstat(root)
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"release source root does not exist: {root}") from exc
    if stat.S_ISLNK(root_status.st_mode):
        raise SymlinkNotAllowed(f"release source root must not be a symlink: {root}")
    if not stat.S_ISDIR(root_status.st_mode):
        raise NotADirectoryError(f"release source root is not a directory: {root}")

    try:
        descriptor = os.open(root, _directory_open_flags())
    except OSError as exc:
        raise ReleaseSourceError(
            f"unable to open release source root safely: {root}"
        ) from exc
    try:
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise ReleaseSourceError(
                f"release source root changed while opening: {root}"
            )
        yield descriptor
    finally:
        os.close(descriptor)


def _walk_tree_from_root_descriptor(
    root_descriptor: int,
    *,
    ignored_root_names: frozenset[str] = frozenset(),
) -> Iterable[_TreeNode]:
    """Walk a retained root descriptor with ``openat``-style child access."""
    root_witness = os.fstat(root_descriptor)

    def visit(
        directory_descriptor: int,
        prefix: str,
        expected_node: _TreeNode | None,
    ) -> Iterable[_TreeNode]:
        if expected_node is not None:
            _assert_node_identity(expected_node, os.fstat(directory_descriptor))
        with os.scandir(directory_descriptor) as entries:
            names = sorted(
                (entry.name for entry in entries),
                key=lambda name: name.encode("utf-8"),
            )
        for name in names:
            if not prefix and name in ignored_root_names:
                continue
            relative_path = f"{prefix}/{name}" if prefix else name
            _validate_relative_path(relative_path)
            status = os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
            node = _tree_node(relative_path, status)
            yield node
            if node.file_type != "directory":
                continue
            child_descriptor = _open_directory_at(
                directory_descriptor,
                name,
                relative_path,
            )
            try:
                _assert_node_identity(node, os.fstat(child_descriptor))
                yield from visit(child_descriptor, relative_path, node)
            finally:
                os.close(child_descriptor)
        if expected_node is not None:
            _assert_node_identity(expected_node, os.fstat(directory_descriptor))

    yield from visit(root_descriptor, "", None)
    _assert_status_unchanged(root_witness, os.fstat(root_descriptor), "<root>")


def _tree_node(relative_path: str, status: os.stat_result) -> _TreeNode:
    """Convert a non-following ``stat`` result into a stable tree node."""
    mode = stat.S_IMODE(status.st_mode)
    if stat.S_ISLNK(status.st_mode):
        file_type: FileType | Literal["symlink", "other"] = "symlink"
    elif stat.S_ISDIR(status.st_mode):
        file_type = "directory"
    elif stat.S_ISREG(status.st_mode):
        file_type = "file"
    else:
        file_type = "other"
    return _TreeNode(
        relative_path=relative_path,
        file_type=file_type,
        mode=mode,
        device=status.st_dev,
        inode=status.st_ino,
        size=status.st_size,
        mtime_ns=status.st_mtime_ns,
        ctime_ns=status.st_ctime_ns,
    )


def _directory_open_flags() -> int:
    """Return flags required to bind recursive traversal to directory fds."""
    directory_flag = getattr(os, "O_DIRECTORY", None)
    if directory_flag is None:
        raise ReleaseSourceError("platform lacks O_DIRECTORY for safe source traversal")
    return (
        os.O_RDONLY | directory_flag | _no_follow_flag() | getattr(os, "O_CLOEXEC", 0)
    )


def _no_follow_flag() -> int:
    """Require a platform primitive that rejects symbolic-link traversal."""
    no_follow = getattr(os, "O_NOFOLLOW", None)
    if no_follow is None:
        raise ReleaseSourceError("platform lacks O_NOFOLLOW for safe source traversal")
    return no_follow


def _open_directory_at(
    parent_descriptor: int,
    name: str,
    relative_path: str,
) -> int:
    """Open a direct child directory without resolving a symbolic link."""
    try:
        return os.open(name, _directory_open_flags(), dir_fd=parent_descriptor)
    except OSError as exc:
        _raise_open_failure(parent_descriptor, name, relative_path, exc)
        raise AssertionError("_raise_open_failure always raises")


def _raise_open_failure(
    parent_descriptor: int,
    name: str,
    relative_path: str,
    original_error: OSError,
) -> None:
    """Classify a failed non-following open without trusting a pathname root."""
    try:
        current_status = os.stat(
            name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
    except OSError:
        raise ReleaseSourceError(
            f"release source changed while opening: {relative_path}"
        ) from original_error
    if stat.S_ISLNK(current_status.st_mode):
        raise SymlinkNotAllowed(
            f"symlink encountered while opening release source: {relative_path}"
        ) from original_error
    raise ReleaseSourceError(
        f"release source changed while opening: {relative_path}"
    ) from original_error


def _open_regular_node(root_descriptor: int, node: _TreeNode) -> int:
    """Open an inspected regular node through the retained root descriptor."""
    if node.file_type != "file":
        raise AssertionError("only regular nodes can be opened as files")
    parent_descriptor, name = _open_parent_descriptor(root_descriptor, node)
    try:
        try:
            descriptor = os.open(
                name,
                os.O_RDONLY | _no_follow_flag() | getattr(os, "O_CLOEXEC", 0),
                dir_fd=parent_descriptor,
            )
        except OSError as exc:
            _raise_open_failure(parent_descriptor, name, node.relative_path, exc)
            raise AssertionError("_raise_open_failure always raises")
        try:
            _assert_node_identity(node, os.fstat(descriptor))
        except Exception:
            os.close(descriptor)
            raise
        return descriptor
    finally:
        os.close(parent_descriptor)


def _open_directory_node(root_descriptor: int, node: _TreeNode) -> int:
    """Open an inspected directory through the retained root descriptor."""
    if node.file_type != "directory":
        raise AssertionError("only directory nodes can be opened as directories")
    parent_descriptor, name = _open_parent_descriptor(root_descriptor, node)
    try:
        descriptor = _open_directory_at(
            parent_descriptor,
            name,
            node.relative_path,
        )
        try:
            _assert_node_identity(node, os.fstat(descriptor))
        except Exception:
            os.close(descriptor)
            raise
        return descriptor
    finally:
        os.close(parent_descriptor)


def _open_parent_descriptor(root_descriptor: int, node: _TreeNode) -> tuple[int, str]:
    """Return a duplicated parent directory fd and leaf name for ``node``."""
    parts = PurePosixPath(node.relative_path).parts
    if not parts:
        raise AssertionError("tree nodes must have a relative path")
    parent_descriptor = os.dup(root_descriptor)
    prefix: list[str] = []
    try:
        for part in parts[:-1]:
            prefix.append(part)
            child_descriptor = _open_directory_at(
                parent_descriptor,
                part,
                "/".join(prefix),
            )
            os.close(parent_descriptor)
            parent_descriptor = child_descriptor
    except Exception:
        os.close(parent_descriptor)
        raise
    return parent_descriptor, parts[-1]


def _assert_node_identity(node: _TreeNode, status: os.stat_result) -> None:
    """Reject replacement or permission changes after the initial inspection."""
    expected_type = (
        stat.S_ISREG(status.st_mode)
        if node.file_type == "file"
        else stat.S_ISDIR(status.st_mode)
    )
    if (
        not expected_type
        or status.st_dev != node.device
        or status.st_ino != node.inode
        or stat.S_IMODE(status.st_mode) != node.mode
        or status.st_size != node.size
        or status.st_mtime_ns != node.mtime_ns
        or status.st_ctime_ns != node.ctime_ns
    ):
        raise ReleaseSourceError(
            f"release source changed during traversal: {node.relative_path}"
        )


def _assert_status_unchanged(
    expected: os.stat_result,
    actual: os.stat_result,
    relative_path: str,
) -> None:
    """Reject a root-directory mutation not represented by a tree node."""
    if not _same_status_witness(expected, actual):
        raise ReleaseSourceError(
            f"release source changed during traversal: {relative_path}"
        )


def _same_node_identity(first: os.stat_result, second: os.stat_result) -> bool:
    """Compare a filesystem object's device and inode without path following."""
    return first.st_dev == second.st_dev and first.st_ino == second.st_ino


def _same_status_witness(first: os.stat_result, second: os.stat_result) -> bool:
    """Compare identity and metadata capable of witnessing a concurrent write."""
    return (
        _same_node_identity(first, second)
        and stat.S_IMODE(first.st_mode) == stat.S_IMODE(second.st_mode)
        and first.st_size == second.st_size
        and first.st_mtime_ns == second.st_mtime_ns
        and first.st_ctime_ns == second.st_ctime_ns
    )


def _validate_safe_mode(node: _TreeNode) -> None:
    """Reject set-ID and sticky bits rather than propagating them into S."""
    if node.mode & 0o7000:
        raise UnsafePermissionError(
            f"special permission bits are not permitted in release source: "
            f"{node.relative_path}"
        )


def _validate_root_mode(status: os.stat_result, root: Path) -> None:
    """Reject non-portable special bits on the source root itself."""
    if stat.S_IMODE(status.st_mode) & 0o7000:
        raise UnsafePermissionError(
            f"release source root has special permission bits: {root}"
        )


def _validate_relative_path(value: str) -> None:
    if not value or "\x00" in value or "\\" in value:
        raise PolicySyntaxError(
            f"path must be a non-empty portable relative path: {value!r}"
        )
    parsed = PurePosixPath(value)
    if (
        parsed.is_absolute()
        or parsed.as_posix() != value
        or any(part in {"", ".", ".."} for part in parsed.parts)
    ):
        raise PolicySyntaxError(f"path must be a portable relative path: {value!r}")


def _validate_relative_pattern(pattern: str, path: Path, line_number: int) -> None:
    try:
        _validate_relative_path(pattern)
    except PolicySyntaxError as exc:
        raise PolicySyntaxError(
            f"{path}:{line_number}: pattern must be portable and relative: {pattern!r}"
        ) from exc


@cache
def _glob_matches(relative_path: str, pattern: str) -> bool:
    """Match POSIX path segments without letting ``*`` cross a slash."""
    import fnmatch

    path_parts = tuple(relative_path.split("/"))
    pattern_parts = tuple(pattern.split("/"))

    @cache
    def matches(path_index: int, pattern_index: int) -> bool:
        if pattern_index == len(pattern_parts):
            return path_index == len(path_parts)
        current_pattern = pattern_parts[pattern_index]
        if current_pattern == "**":
            return any(
                matches(candidate_index, pattern_index + 1)
                for candidate_index in range(path_index, len(path_parts) + 1)
            )
        return (
            path_index < len(path_parts)
            and fnmatch.fnmatchcase(path_parts[path_index], current_pattern)
            and matches(path_index + 1, pattern_index + 1)
        )

    return matches(0, 0)


def _sha256_regular_node(root_descriptor: int, node: _TreeNode) -> str:
    """Hash an inspected regular node through the retained root descriptor."""
    descriptor = _open_regular_node(root_descriptor, node)
    try:
        digest = hashlib.sha256()
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
        _assert_node_identity(node, os.fstat(descriptor))
        return digest.hexdigest()
    finally:
        os.close(descriptor)


def _write_all(descriptor: int, payload: bytes) -> None:
    """Write a complete payload even when the operating system short-writes."""
    remaining = memoryview(payload)
    while remaining:
        written = os.write(descriptor, remaining)
        if written <= 0:
            raise OSError("failed to write detached manifest")
        remaining = remaining[written:]


def _validate_manifest_entry(entry: ManifestEntry, index: int) -> None:
    try:
        _validate_relative_path(entry.path)
    except PolicySyntaxError as exc:
        raise ManifestFormatError(f"manifest entry {index} has invalid path") from exc
    if entry.file_type not in {"file", "directory"}:
        raise ManifestFormatError(f"manifest entry {index} has invalid file type")
    if len(entry.mode) != 4 or any(
        character not in "01234567" for character in entry.mode
    ):
        raise ManifestFormatError(f"manifest entry {index} has invalid mode")
    if int(entry.mode, 8) & 0o7000:
        raise ManifestFormatError(
            f"manifest entry {index} has unsafe special permissions"
        )
    allowed_modes = {"0755"} if entry.file_type == "directory" else {"0644", "0755"}
    if entry.mode not in allowed_modes:
        raise ManifestFormatError(
            f"manifest entry {index} has non-canonical portable mode"
        )
    if entry.file_type == "file" and (
        len(entry.sha256) != 64
        or any(character not in "0123456789abcdef" for character in entry.sha256)
    ):
        raise ManifestFormatError(f"manifest entry {index} has invalid file SHA-256")
    if entry.file_type == "directory" and entry.sha256:
        raise ManifestFormatError(
            f"manifest entry {index} gives a directory a file SHA-256"
        )
    if entry.symlink_target:
        raise ManifestFormatError(
            f"manifest entry {index} contains a forbidden symlink target"
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("build", "verify"):
        command = commands.add_parser(name)
        command.add_argument("--root", type=Path, required=True)
        command.add_argument("--allowlist", type=Path, required=True)
        command.add_argument(
            "--public-checkout",
            action="store_true",
            help="ignore only local .git metadata while building or verifying M(P)",
        )
        command.add_argument("--json", action="store_true")
        if name == "build":
            command.add_argument("--output", type=Path)
        else:
            command.add_argument("--expected", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the stable build/verify command-line interface."""
    args = _parser().parse_args(argv)
    try:
        policy = load_policy(args.allowlist)
        manifest_builder = (
            build_public_checkout_manifest
            if args.public_checkout
            else build_manifest
        )
        if args.command == "build":
            manifest = manifest_builder(args.root, policy)
            if args.output is not None:
                write_manifest(manifest, args.output)
            _emit_summary(manifest, args.json, output=args.output)
            if args.output is None and not args.json:
                sys.stdout.buffer.write(manifest.to_bytes())
            return 0
        actual = _verify_manifest(args.root, policy, args.expected, manifest_builder)
        _emit_summary(actual, args.json, output=None)
        return 0
    except (OSError, ReleaseSourceError, ValueError) as exc:
        print(f"release-source: error: {exc}", file=sys.stderr)
        return 2


def _emit_summary(
    manifest: ReleaseSourceManifest,
    as_json: bool,
    output: Path | None,
) -> None:
    payload = {
        "entries": len(manifest.entries),
        "manifest_sha256": manifest_digest(manifest),
    }
    if output is not None:
        payload["output"] = output.as_posix()
    if as_json:
        print(
            json.dumps(
                payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")
            )
        )
    else:
        summary = (
            f"entries={payload['entries']} manifest_sha256={payload['manifest_sha256']}"
        )
        print(
            summary,
            file=sys.stderr,
        )


if __name__ == "__main__":
    raise SystemExit(main())
