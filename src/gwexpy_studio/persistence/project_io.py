"""Versioned JSON project persistence interface."""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from os import PathLike
from pathlib import Path
from typing import Any

from ..domain.project import Project
from ..errors import ProjectFormatError
from .atomic import atomic_write_bytes, ensure_directory

PROJECT_SIZE_LIMIT_BYTES = 16 * 1024 * 1024
MAX_GRAPH_DEPTH = 64


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Reject duplicate dictionary keys during JSON deserialization."""
    res: dict[str, Any] = {}
    for k, v in pairs:
        if k in res:
            raise ProjectFormatError(f"Duplicate JSON key: {k!r}")
        res[k] = v
    return res


def _reject_constant(constant: str) -> None:
    """Reject non-finite JSON constants (NaN, Infinity, -Infinity)."""
    raise ProjectFormatError(f"Non-finite JSON constant encountered: {constant!r}")


def _bounded_json_depth(value: Any, *, limit: int) -> int:
    """Return the structural JSON depth of value using an explicit stack.

    Mirrors the recursive depth definition used across this codebase (every
    nested dict/list adds one level, a leaf contributes zero) without ever
    recursing, so a document far beyond ``limit`` cannot raise
    ``RecursionError`` before the depth limit is enforced. The walk stops
    as soon as the running depth exceeds ``limit`` — callers only need to
    distinguish "at or under the limit" from "over it", not the exact depth
    of a pathologically deep document.
    """
    if not isinstance(value, (dict, list, tuple, Mapping)):
        return 0

    max_depth = 1
    stack: list[tuple[Any, int]] = [(value, 1)]
    while stack:
        node, level = stack.pop()
        children = node.values() if isinstance(node, (dict, Mapping)) else node
        for child in children:
            if isinstance(child, (dict, list, tuple, Mapping)):
                child_level = level + 1
                if child_level > max_depth:
                    max_depth = child_level
                    if max_depth > limit:
                        return max_depth
                stack.append((child, child_level))
    return max_depth


def _check_finite_recursive(value: Any) -> None:
    """Ensure no float NaN or infinite values exist in data structures."""
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ProjectFormatError(f"Non-finite float value encountered: {value}")
    elif isinstance(value, Mapping):
        for k, v in value.items():
            if not isinstance(k, str):
                raise ProjectFormatError(f"Dictionary key must be string: {k!r}")
            _check_finite_recursive(v)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _check_finite_recursive(item)


def validate_project_document(document: dict[str, Any]) -> None:
    """Validate a manifest against JSON constraints, depth limit, and domain model."""
    if not isinstance(document, Mapping):
        raise ProjectFormatError("Project document must be a mapping")

    # 0. Check structural depth before any recursive walk of the document
    # (in particular before _check_finite_recursive below, which is
    # itself unbounded recursion) so a pathologically deep document is
    # rejected here instead of overflowing the call stack later.
    depth = _bounded_json_depth(document, limit=MAX_GRAPH_DEPTH)
    if depth > MAX_GRAPH_DEPTH:
        raise ProjectFormatError(
            f"Project document depth {depth} exceeds limit {MAX_GRAPH_DEPTH}"
        )

    _check_finite_recursive(document)

    # Validate against domain aggregate model
    project = Project.from_dict(document)
    project.validate()


def save_project(
    project: Project,
    path: str | PathLike[str],
    *,
    clock: Callable[[], str] | None = None,
) -> None:
    """Atomically save a data-only project to a versioned .gwxproj file."""
    path_obj = Path(path).resolve()
    target_dir = path_obj.parent
    ensure_directory(target_dir)

    timestamp = clock() if clock is not None else datetime.now(UTC).isoformat()
    old_modified = project.modified

    # Build and validate manifest
    document = project.to_dict()
    document["modified"] = timestamp
    validate_project_document(document)

    raw_bytes = (
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")

    if len(raw_bytes) > PROJECT_SIZE_LIMIT_BYTES:
        raise ProjectFormatError(
            f"Serialized project size ({len(raw_bytes)} bytes) exceeds limit "
            f"({PROJECT_SIZE_LIMIT_BYTES} bytes)"
        )

    try:
        atomic_write_bytes(path_obj, raw_bytes)
        project.modified = timestamp
    except BaseException:
        project.modified = old_modified
        raise


def load_project(path: str | PathLike[str]) -> Project:
    """Load, strictly validate, and deserialize a data-only project file."""
    path_obj = Path(path)
    if not path_obj.is_file():
        raise ProjectFormatError(f"Project file does not exist: {path_obj}")

    size = path_obj.stat().st_size
    if size > PROJECT_SIZE_LIMIT_BYTES:
        raise ProjectFormatError(
            f"Project file size ({size} bytes) exceeds limit "
            f"({PROJECT_SIZE_LIMIT_BYTES} bytes)"
        )

    with path_obj.open("rb") as stream:
        raw = stream.read(PROJECT_SIZE_LIMIT_BYTES + 1)
    if len(raw) > PROJECT_SIZE_LIMIT_BYTES:
        raise ProjectFormatError("Project file size exceeds limit during read")
    try:
        content = raw.decode("utf-8")
        document = json.loads(
            content,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except json.JSONDecodeError as exc:
        raise ProjectFormatError(f"Malformed project JSON: {exc}") from exc
    except UnicodeError as exc:
        raise ProjectFormatError("Project file must contain valid UTF-8") from exc
    except RecursionError as exc:
        # CPython's C JSON scanner still recurses once per nesting level
        # internally, so a pathologically deep document can blow the call
        # stack before ``validate_project_document``'s own iterative depth
        # check ever runs. Treat that exactly like a malformed document
        # instead of letting RecursionError escape.
        raise ProjectFormatError(
            "Project file nesting is too deep to parse as JSON"
        ) from exc

    validate_project_document(document)
    return Project.from_dict(document)
