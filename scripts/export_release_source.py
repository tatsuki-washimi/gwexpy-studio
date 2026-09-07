"""Export a deterministic public release-source snapshot from a private tree."""

from __future__ import annotations

import argparse
import ctypes
import errno
import hashlib
import json
import os
import secrets
import stat
import sys
from pathlib import Path

try:  # Support both ``python scripts/...`` and ``import scripts...``.
    from .release_source_manifest import (
        DeniedPathError,
        ReleaseSourceError,
        ReleaseSourceManifest,
        ReleaseSourcePolicy,
        SymlinkNotAllowed,
        UnclassifiedPathError,
        UnexpectedFileTypeError,
        UnsafePermissionError,
        _assert_node_identity,
        _assert_status_unchanged,
        _build_manifest_from_root_descriptor,
        _directory_open_flags,
        _no_follow_flag,
        _open_directory_node,
        _open_regular_node,
        _open_root_directory,
        _TreeNode,
        _validate_root_mode,
        _walk_tree,
        _walk_tree_from_root_descriptor,
        _write_manifest_at,
        canonical_source_mode,
        load_policy,
        manifest_digest,
    )
    from .verify_public_source import scan_public_checkout
except ImportError:  # pragma: no cover - exercised by direct CLI invocation.
    from release_source_manifest import (  # type: ignore[no-redef]
        DeniedPathError,
        ReleaseSourceError,
        ReleaseSourceManifest,
        ReleaseSourcePolicy,
        SymlinkNotAllowed,
        UnclassifiedPathError,
        UnexpectedFileTypeError,
        UnsafePermissionError,
        _assert_node_identity,
        _assert_status_unchanged,
        _build_manifest_from_root_descriptor,
        _directory_open_flags,
        _no_follow_flag,
        _open_directory_node,
        _open_regular_node,
        _open_root_directory,
        _TreeNode,
        _validate_root_mode,
        _walk_tree,
        _walk_tree_from_root_descriptor,
        _write_manifest_at,
        canonical_source_mode,
        load_policy,
        manifest_digest,
    )
    from verify_public_source import scan_public_checkout  # type: ignore[no-redef]


def export_release_source(
    source_root: Path,
    snapshot_root: Path,
    policy: ReleaseSourcePolicy,
    detached_manifest: Path,
) -> ReleaseSourceManifest:
    """Copy the explicitly public subset of ``source_root`` into ``snapshot_root``.

    The detached manifest is deliberately required to live outside the
    snapshot. This avoids a self-reference while preserving a byte-for-byte
    identity proof that can later be checked against a public checkout.
    """
    source = _resolve_source_root(source_root)
    snapshot = _absolute_path(snapshot_root)
    sidecar = _absolute_path(detached_manifest)

    with _open_root_directory(source) as source_descriptor:
        _validate_root_mode(os.fstat(source_descriptor), source)
        _validate_export_locations(
            source,
            snapshot,
            sidecar,
            source_descriptor=source_descriptor,
        )
        source_root_status = os.fstat(source_descriptor)
        source_nodes = tuple(_walk_tree_from_root_descriptor(source_descriptor))
        selected_nodes = _select_nodes(source, policy, nodes=source_nodes)
        with _open_root_directory(snapshot.parent) as snapshot_parent_descriptor:
            _reject_source_output_parent(
                snapshot_parent_descriptor,
                source_descriptor,
                "snapshot directory",
            )
            with _open_root_directory(sidecar.parent) as sidecar_parent_descriptor:
                _reject_source_output_parent(
                    sidecar_parent_descriptor,
                    source_descriptor,
                    "detached manifest",
                )
                stage_name, stage_descriptor, stage_status = _create_staging_directory(
                    snapshot_parent_descriptor,
                    snapshot.name,
                )
                sidecar_status: os.stat_result | None = None
                try:
                    _assert_staging_root_bound(
                        snapshot_parent_descriptor,
                        stage_name,
                        stage_descriptor,
                        stage_status,
                        "source copy",
                    )
                    copied_hashes = _copy_selected_nodes(
                        selected_nodes,
                        source_descriptor,
                        stage_descriptor,
                    )
                    _assert_source_nodes_stable(source_descriptor, source_nodes)
                    _assert_status_unchanged(
                        source_root_status,
                        os.fstat(source_descriptor),
                        "<root>",
                    )
                    _assert_staging_root_bound(
                        snapshot_parent_descriptor,
                        stage_name,
                        stage_descriptor,
                        stage_status,
                        "manifest creation",
                    )
                    manifest = _build_manifest_from_root_descriptor(
                        stage_descriptor,
                        policy,
                    )
                    _validate_copied_hashes(manifest, copied_hashes)
                    scanned_manifest = scan_public_checkout(
                        snapshot.parent / stage_name,
                        policy,
                    )
                    if scanned_manifest != manifest:
                        raise ReleaseSourceError(
                            "staging tree changed during public content scan"
                        )
                    _assert_staging_root_bound(
                        snapshot_parent_descriptor,
                        stage_name,
                        stage_descriptor,
                        stage_status,
                        "detached evidence creation",
                    )
                    sidecar_status = _write_sidecar(
                        manifest,
                        sidecar_parent_descriptor,
                        sidecar,
                    )
                    _assert_staging_root_bound(
                        snapshot_parent_descriptor,
                        stage_name,
                        stage_descriptor,
                        stage_status,
                        "detached evidence binding",
                    )
                    _assert_parent_path_matches_descriptor(
                        sidecar.parent,
                        sidecar_parent_descriptor,
                        "detached manifest",
                    )
                    _assert_sidecar_stable(
                        sidecar_parent_descriptor,
                        sidecar.name,
                        sidecar_status,
                    )
                    _assert_staging_root_bound(
                        snapshot_parent_descriptor,
                        stage_name,
                        stage_descriptor,
                        stage_status,
                        "final manifest creation",
                    )
                    if (
                        _build_manifest_from_root_descriptor(stage_descriptor, policy)
                        != manifest
                    ):
                        raise ReleaseSourceError(
                            "staging tree changed after detached evidence was written"
                        )
                    _assert_parent_path_matches_descriptor(
                        snapshot.parent,
                        snapshot_parent_descriptor,
                        "snapshot directory",
                    )
                    _assert_sidecar_stable(
                        sidecar_parent_descriptor,
                        sidecar.name,
                        sidecar_status,
                    )
                    _assert_staging_root_bound(
                        snapshot_parent_descriptor,
                        stage_name,
                        stage_descriptor,
                        stage_status,
                        "publication",
                    )
                    _publish_staging_directory(
                        snapshot_parent_descriptor,
                        stage_name,
                        snapshot.name,
                    )
                    _assert_parent_path_matches_descriptor(
                        snapshot.parent,
                        snapshot_parent_descriptor,
                        "snapshot directory",
                    )
                    _assert_published_snapshot(
                        snapshot_parent_descriptor,
                        snapshot.name,
                        stage_descriptor,
                        stage_status,
                    )
                finally:
                    os.close(stage_descriptor)
    return manifest


def _resolve_source_root(source_root: Path) -> Path:
    source = _absolute_path(source_root)
    if source.is_symlink():
        raise SymlinkNotAllowed(f"release source root must not be a symlink: {source}")
    if not source.exists():
        raise FileNotFoundError(f"release source root does not exist: {source}")
    if not source.is_dir():
        raise NotADirectoryError(f"release source root is not a directory: {source}")
    return source


def _absolute_path(path: Path) -> Path:
    """Make a lexical absolute path without resolving attacker-controlled links."""
    return Path(os.path.abspath(path))


def _descriptor_path(descriptor: int) -> Path:
    """Return the Linux kernel path bound to an already-open root descriptor."""
    try:
        target = os.readlink(f"/proc/self/fd/{descriptor}")
    except OSError as exc:
        raise ReleaseSourceError(
            "cannot establish the opened source root identity"
        ) from exc
    if target.endswith(" (deleted)"):
        raise ReleaseSourceError("release source root was deleted during export")
    return Path(target)


def _validate_export_locations(
    source: Path,
    snapshot: Path,
    sidecar: Path,
    *,
    source_descriptor: int | None = None,
) -> None:
    """Reject outputs that would write into the source through any known path."""
    if snapshot.exists() or snapshot.is_symlink():
        raise FileExistsError(f"refusing to overwrite snapshot directory: {snapshot}")
    if _is_within(snapshot, source):
        raise ValueError("snapshot directory must be outside the source tree")
    if _is_within(sidecar, source):
        raise ValueError("detached manifest must be outside the source tree")
    if _is_within(sidecar, snapshot):
        raise ValueError("detached manifest must be outside the snapshot directory")
    if sidecar.exists() or sidecar.is_symlink():
        raise FileExistsError(f"refusing to overwrite detached manifest: {sidecar}")
    for path, label in (
        (snapshot, "snapshot directory"),
        (sidecar, "detached manifest"),
    ):
        if not path.parent.exists():
            raise FileNotFoundError(f"{label} parent directory must already exist")
        if path.parent.is_symlink():
            raise SymlinkNotAllowed(
                f"{label} parent directory must not be a symlink: {path.parent}"
            )
    if source_descriptor is None:
        return
    physical_source = _descriptor_path(source_descriptor)
    for candidate, label in (
        (snapshot, "snapshot directory"),
        (sidecar, "detached manifest"),
    ):
        if _is_within(candidate.resolve(strict=False), physical_source):
            raise ValueError(f"{label} must be outside the source tree")


def _reject_source_output_parent(
    parent_descriptor: int,
    source_descriptor: int,
    label: str,
) -> None:
    """Recheck an opened output parent against the pinned source root."""
    if _is_within(
        _descriptor_path(parent_descriptor),
        _descriptor_path(source_descriptor),
    ):
        raise ValueError(f"{label} must be outside the source tree")


def _assert_parent_path_matches_descriptor(
    parent_path: Path,
    expected_descriptor: int,
    label: str,
) -> None:
    """Fail if an output parent pathname was replaced after it was opened."""
    with _open_root_directory(parent_path) as current_descriptor:
        if not _same_identity(
            os.fstat(current_descriptor),
            os.fstat(expected_descriptor),
        ):
            raise ReleaseSourceError(f"{label} parent changed during export")


def _assert_sidecar_stable(
    parent_descriptor: int,
    name: str,
    expected_status: os.stat_result,
) -> None:
    """Bind the evidence leaf to the file created through its retained parent."""
    try:
        current_status = os.stat(
            name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
    except OSError as exc:
        raise ReleaseSourceError("detached manifest disappeared during export") from exc
    if not stat.S_ISREG(current_status.st_mode) or not _same_witness(
        current_status,
        expected_status,
    ):
        raise ReleaseSourceError("detached manifest changed during export")


def _write_sidecar(
    manifest: ReleaseSourceManifest,
    parent_descriptor: int,
    sidecar: Path,
) -> os.stat_result:
    """Write the evidence leaf through the retained, prevalidated parent fd."""
    return _write_manifest_at(manifest, parent_descriptor, sidecar.name, sidecar)


def _select_nodes(
    source: Path,
    policy: ReleaseSourcePolicy,
    *,
    nodes: tuple[_TreeNode, ...] | None = None,
) -> tuple[_TreeNode, ...]:
    """Select policy-approved nodes from one already-inspected source tree."""
    if nodes is None:
        nodes = tuple(_walk_tree(source))
    by_path = {node.relative_path: node for node in nodes}
    included_paths: set[str] = set()

    for node in nodes:
        decision = policy.classify(node.relative_path)
        if decision == "include":
            _ensure_copyable(node)
            included_paths.add(node.relative_path)
            _reject_denied_ancestor(node.relative_path, policy)
            continue
        if decision is None:
            if node.file_type == "symlink":
                raise SymlinkNotAllowed(
                    f"unclassified symlink in source tree: {node.relative_path}"
                )
            if node.file_type == "other":
                raise UnexpectedFileTypeError(
                    f"unclassified unsupported file type: {node.relative_path}"
                )

    for node in nodes:
        decision = policy.classify(node.relative_path)
        if decision == "include":
            continue
        is_container = node.file_type == "directory" and any(
            path.startswith(f"{node.relative_path}/") for path in included_paths
        )
        if is_container:
            continue
        if decision == "deny" or decision == "exclude":
            continue
        if node.file_type == "symlink":
            raise SymlinkNotAllowed(
                f"unclassified symlink in source tree: {node.relative_path}"
            )
        if node.file_type == "other":
            raise UnexpectedFileTypeError(
                f"unclassified unsupported file type: {node.relative_path}"
            )
        raise UnclassifiedPathError(
            f"unclassified path in source tree: {node.relative_path}"
        )

    selected_paths = set(included_paths)
    for path in tuple(included_paths):
        parent = Path(path).parent
        while parent != Path("."):
            selected_paths.add(parent.as_posix())
            parent = parent.parent
    selected_nodes = tuple(
        sorted(
            (by_path[path] for path in selected_paths),
            key=lambda node: (
                node.relative_path.count("/"),
                node.relative_path.encode("utf-8"),
            ),
        )
    )
    for node in selected_nodes:
        _ensure_copyable(node)
    return selected_nodes


def _ensure_copyable(node: _TreeNode) -> None:
    if node.mode & 0o7000:
        raise UnsafePermissionError(
            f"special permission bits are not permitted in release source: "
            f"{node.relative_path}"
        )
    if node.file_type == "symlink":
        raise SymlinkNotAllowed(
            f"symlinks are not permitted in release source: {node.relative_path}"
        )
    if node.file_type == "other":
        raise UnexpectedFileTypeError(
            f"unsupported file type in release source: {node.relative_path}"
        )


def _reject_denied_ancestor(relative_path: str, policy: ReleaseSourcePolicy) -> None:
    ancestor = Path(relative_path).parent
    while ancestor != Path("."):
        if policy.classify(ancestor.as_posix()) == "deny":
            raise DeniedPathError(
                f"included path has a denied private ancestor: {relative_path}"
            )
        ancestor = ancestor.parent


def _copy_selected_nodes(
    nodes: tuple[_TreeNode, ...],
    source_descriptor: int,
    snapshot_descriptor: int,
) -> dict[str, str]:
    """Copy inspected nodes through retained source and staging directory fds."""
    directories = [node for node in nodes if node.file_type == "directory"]
    files = [node for node in nodes if node.file_type == "file"]
    expected_directories: dict[str, os.stat_result] = {}
    for node in directories:
        source_directory = _open_directory_node(source_descriptor, node)
        try:
            _assert_node_identity(node, os.fstat(source_directory))
        finally:
            os.close(source_directory)
        expected_directories[node.relative_path] = _make_snapshot_directory(
            snapshot_descriptor,
            node.relative_path,
            expected_directories,
        )

    copied_hashes: dict[str, str] = {}
    for node in files:
        copied_hashes[node.relative_path] = _copy_regular_file(
            source_descriptor,
            node,
            snapshot_descriptor,
            expected_directories,
        )
    for node in reversed(directories):
        destination_directory = _open_snapshot_directory(
            snapshot_descriptor,
            node.relative_path,
            expected_directories,
        )
        try:
            _assert_snapshot_directory_stable(
                node.relative_path,
                expected_directories[node.relative_path],
                os.fstat(destination_directory),
            )
            os.fchmod(
                destination_directory,
                canonical_source_mode("directory", node.mode),
            )
            expected_directories[node.relative_path] = os.fstat(destination_directory)
        finally:
            os.close(destination_directory)
    return copied_hashes


def _copy_regular_file(
    source_root_descriptor: int,
    node: _TreeNode,
    snapshot_root_descriptor: int,
    expected_directories: dict[str, os.stat_result],
) -> str:
    """Copy and hash one pinned source file without traversing source paths."""
    source_descriptor = _open_regular_node(source_root_descriptor, node)
    try:
        destination_parent, destination_name = _open_snapshot_parent(
            snapshot_root_descriptor,
            node.relative_path,
            expected_directories,
        )
        try:
            destination_descriptor = os.open(
                destination_name,
                (
                    os.O_WRONLY
                    | os.O_CREAT
                    | os.O_EXCL
                    | _no_follow_flag()
                    | getattr(os, "O_CLOEXEC", 0)
                ),
                0o600,
                dir_fd=destination_parent,
            )
            try:
                digest = hashlib.sha256()
                while chunk := os.read(source_descriptor, 1024 * 1024):
                    digest.update(chunk)
                    _write_all(destination_descriptor, chunk)
                _assert_node_identity(node, os.fstat(source_descriptor))
                os.fchmod(
                    destination_descriptor,
                    canonical_source_mode("file", node.mode),
                )
                parent_path = Path(node.relative_path).parent.as_posix()
                if parent_path != ".":
                    expected_directories[parent_path] = os.fstat(destination_parent)
                return digest.hexdigest()
            finally:
                os.close(destination_descriptor)
        finally:
            os.close(destination_parent)
    finally:
        os.close(source_descriptor)


def _make_snapshot_directory(
    snapshot_descriptor: int,
    relative_path: str,
    expected_directories: dict[str, os.stat_result],
) -> os.stat_result:
    """Create one staging directory with private temporary permissions."""
    parent_descriptor, name = _open_snapshot_parent(
        snapshot_descriptor,
        relative_path,
        expected_directories,
    )
    try:
        os.mkdir(name, 0o700, dir_fd=parent_descriptor)
        parent_path = Path(relative_path).parent.as_posix()
        if parent_path != ".":
            expected_directories[parent_path] = os.fstat(parent_descriptor)
    finally:
        os.close(parent_descriptor)
    destination_directory = _open_snapshot_directory(
        snapshot_descriptor,
        relative_path,
        expected_directories,
    )
    try:
        return os.fstat(destination_directory)
    finally:
        os.close(destination_directory)


def _open_snapshot_directory(
    snapshot_descriptor: int,
    relative_path: str,
    expected_directories: dict[str, os.stat_result],
) -> int:
    """Open one already-created staging directory without following links."""
    parent_descriptor, name = _open_snapshot_parent(
        snapshot_descriptor,
        relative_path,
        expected_directories,
    )
    try:
        try:
            return os.open(
                name,
                _directory_open_flags(),
                dir_fd=parent_descriptor,
            )
        except OSError as exc:
            raise ReleaseSourceError(
                f"staging directory changed during export: {relative_path}"
            ) from exc
    finally:
        os.close(parent_descriptor)


def _open_snapshot_parent(
    snapshot_descriptor: int,
    relative_path: str,
    expected_directories: dict[str, os.stat_result],
) -> tuple[int, str]:
    """Return a duplicated staging parent descriptor and its leaf name."""
    parts = Path(relative_path).parts
    if not parts:
        raise AssertionError("snapshot path must be non-empty")
    parent_descriptor = os.dup(snapshot_descriptor)
    try:
        prefix: list[str] = []
        for part in parts[:-1]:
            prefix.append(part)
            child_descriptor = os.open(
                part,
                _directory_open_flags(),
                dir_fd=parent_descriptor,
            )
            expected = expected_directories.get("/".join(prefix))
            if expected is None:
                os.close(child_descriptor)
                raise ReleaseSourceError(
                    f"staging parent was not created by export: {relative_path}"
                )
            try:
                _assert_snapshot_directory_stable(
                    "/".join(prefix),
                    expected,
                    os.fstat(child_descriptor),
                )
            except Exception:
                os.close(child_descriptor)
                raise
            os.close(parent_descriptor)
            parent_descriptor = child_descriptor
    except OSError as exc:
        os.close(parent_descriptor)
        raise ReleaseSourceError(
            f"staging parent changed during export: {relative_path}"
        ) from exc
    except Exception:
        os.close(parent_descriptor)
        raise
    return parent_descriptor, parts[-1]


def _assert_snapshot_directory_stable(
    relative_path: str,
    expected: os.stat_result,
    actual: os.stat_result,
) -> None:
    """Reject a staging directory replacement before it receives copied data."""
    if not stat.S_ISDIR(actual.st_mode) or not _same_witness(expected, actual):
        raise ReleaseSourceError(
            f"staging directory changed during export: {relative_path}"
        )


def _write_all(descriptor: int, chunk: bytes) -> None:
    """Write a complete chunk even when the operating system short-writes."""
    remaining = memoryview(chunk)
    while remaining:
        written = os.write(descriptor, remaining)
        if written <= 0:
            raise OSError("failed to write release-source snapshot data")
        remaining = remaining[written:]


def _assert_source_nodes_stable(
    source_descriptor: int,
    expected_nodes: tuple[_TreeNode, ...],
) -> None:
    """Fail closed if the source tree changed between selection and copy."""
    actual_nodes = tuple(_walk_tree_from_root_descriptor(source_descriptor))
    if actual_nodes != expected_nodes:
        raise ReleaseSourceError("release source changed during snapshot export")


def _validate_copied_hashes(
    manifest: ReleaseSourceManifest,
    copied_hashes: dict[str, str],
) -> None:
    """Bind each streamed source copy to the final staging manifest content."""
    manifest_hashes = {
        entry.path: entry.sha256
        for entry in manifest.entries
        if entry.file_type == "file"
    }
    if manifest_hashes != copied_hashes:
        raise ReleaseSourceError(
            "staging tree changed after source copy or has incomplete file coverage"
        )


def _create_staging_directory(
    parent_descriptor: int,
    snapshot_name: str,
) -> tuple[str, int, os.stat_result]:
    """Create a private sibling staging directory without reusing an old path."""
    for _ in range(128):
        stage_name = f".{snapshot_name}.stage-{secrets.token_hex(16)}"
        try:
            os.mkdir(stage_name, 0o700, dir_fd=parent_descriptor)
        except FileExistsError:
            continue
        try:
            named_status = os.stat(
                stage_name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
        except OSError as exc:
            raise ReleaseSourceError(
                "staging root changed before initial binding"
            ) from exc
        _validate_staging_root_status(named_status)
        try:
            descriptor = os.open(
                stage_name,
                _directory_open_flags(),
                dir_fd=parent_descriptor,
            )
        except OSError as exc:
            raise ReleaseSourceError("unable to open staging root safely") from exc
        try:
            opened_status = os.fstat(descriptor)
            _validate_staging_root_status(opened_status)
            if not _same_witness(named_status, opened_status):
                raise ReleaseSourceError("staging root changed during initial binding")
            _assert_empty_staging_root(descriptor)
            _assert_staging_root_bound(
                parent_descriptor,
                stage_name,
                descriptor,
                opened_status,
                "initial binding",
            )
            return stage_name, descriptor, opened_status
        except Exception:
            os.close(descriptor)
            raise
    raise ReleaseSourceError("unable to allocate a unique release-source staging path")


def _publish_staging_directory(
    parent_descriptor: int,
    stage_name: str,
    snapshot_name: str,
) -> None:
    """Atomically publish a staging directory without replacing another output."""
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        renameat2 = libc.renameat2
    except (AttributeError, OSError) as exc:
        raise ReleaseSourceError(
            "atomic no-replace publication requires Linux renameat2"
        ) from exc
    renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    if (
        renameat2(
            parent_descriptor,
            os.fsencode(stage_name),
            parent_descriptor,
            os.fsencode(snapshot_name),
            1,  # RENAME_NOREPLACE
        )
        != 0
    ):
        error_number = ctypes.get_errno()
        if error_number == errno.EEXIST:
            raise FileExistsError(
                f"refusing to overwrite snapshot directory: {snapshot_name}"
            )
        raise OSError(error_number, os.strerror(error_number), snapshot_name)


def _assert_published_snapshot(
    parent_descriptor: int,
    name: str,
    stage_descriptor: int,
    expected_status: os.stat_result,
) -> None:
    """Confirm the final name still identifies the staging directory just moved."""
    descriptor_status = os.fstat(stage_descriptor)
    _validate_staging_root_status(descriptor_status)
    try:
        current_status = os.stat(
            name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
    except OSError as exc:
        raise ReleaseSourceError(
            "published snapshot disappeared during export"
        ) from exc
    _validate_staging_root_status(current_status)
    if (
        not _same_identity(current_status, expected_status)
        or not _same_identity(descriptor_status, expected_status)
    ):
        raise ReleaseSourceError("published snapshot changed during export")


def _validate_staging_root_status(status: os.stat_result) -> None:
    """Reject an unsafe or externally owned stage root before it receives data."""
    if not stat.S_ISDIR(status.st_mode):
        raise ReleaseSourceError("staging root is not a directory")
    mode = stat.S_IMODE(status.st_mode)
    if mode & 0o7000:
        raise ReleaseSourceError("staging root has special permission bits")
    if mode != 0o700:
        raise ReleaseSourceError("staging root must retain private mode 0700")
    if status.st_uid != os.geteuid():
        raise ReleaseSourceError("staging root owner differs from the exporter")


def _assert_empty_staging_root(stage_descriptor: int) -> None:
    """Ensure a newly bound stage root contains no pre-existing victim data."""
    try:
        names = os.listdir(stage_descriptor)
    except OSError as exc:
        raise ReleaseSourceError("unable to inspect staging root safely") from exc
    if names:
        raise ReleaseSourceError("staging root is not empty after creation")


def _assert_staging_root_bound(
    parent_descriptor: int,
    name: str,
    stage_descriptor: int,
    expected_status: os.stat_result,
    phase: str,
) -> None:
    """Bind the stage pathname, retained descriptor, identity, and safe mode."""
    descriptor_status = os.fstat(stage_descriptor)
    _validate_staging_root_status(descriptor_status)
    try:
        current_status = os.stat(
            name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
    except OSError as exc:
        raise ReleaseSourceError(f"staging root disappeared during {phase}") from exc
    _validate_staging_root_status(current_status)
    if (
        not _same_identity(expected_status, descriptor_status)
        or not _same_identity(expected_status, current_status)
    ):
        raise ReleaseSourceError(f"staging root changed during {phase}")


def _same_identity(first: os.stat_result, second: os.stat_result) -> bool:
    """Compare only the stable device/inode fields used for descriptor binding."""
    return first.st_dev == second.st_dev and first.st_ino == second.st_ino


def _same_witness(first: os.stat_result, second: os.stat_result) -> bool:
    """Compare identity and mutation-witness fields for an initial binding."""
    return (
        _same_identity(first, second)
        and stat.S_IMODE(first.st_mode) == stat.S_IMODE(second.st_mode)
        and first.st_size == second.st_size
        and first.st_mtime_ns == second.st_mtime_ns
        and first.st_ctime_ns == second.st_ctime_ns
    )


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--allowlist", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the stable snapshot-export command-line interface."""
    args = _parser().parse_args(argv)
    try:
        policy = load_policy(args.allowlist)
        manifest = export_release_source(
            args.source,
            args.snapshot,
            policy,
            args.manifest,
        )
        payload = {
            "entries": len(manifest.entries),
            "manifest": args.manifest.as_posix(),
            "manifest_sha256": manifest_digest(manifest),
            "snapshot": args.snapshot.as_posix(),
        }
        if args.json:
            print(
                json.dumps(
                    payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")
                )
            )
        else:
            summary = (
                f"entries={payload['entries']} "
                f"manifest_sha256={payload['manifest_sha256']}"
            )
            print(
                summary,
                file=sys.stderr,
            )
        return 0
    except (OSError, ReleaseSourceError, ValueError) as exc:
        print(f"release-source export: error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
