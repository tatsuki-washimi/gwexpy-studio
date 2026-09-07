"""Bounded, format-independent inspection of user-selected native I/O sources."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from collections.abc import Mapping
from importlib.metadata import version
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from .io import identify_io, io_class
from .values import decode_value

DEFAULT_IO_BYTES = 512 * 1024 * 1024
DEFAULT_IO_ENTRIES = 10_000
_MAX_IO_DEPTH = 128
_INSPECTION_PREVIEW_ENTRIES = 128
_INSPECTION_PREVIEW_BYTES = 32 * 1024


def _intake_limit(value: Any, label: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{label} limit must be a positive integer")
    return value


def _intake_path(raw: str) -> Path:
    if not isinstance(raw, str) or not raw:
        raise ValueError("Each source must be a nonempty local path")
    if raw.startswith("file:"):
        url = urlsplit(raw)
        if url.netloc not in ("", "localhost"):
            raise ValueError("File URL must refer to a local source")
        raw = unquote(url.path)
    elif "://" in raw:
        raise ValueError("Select a local file or directory source")
    return Path(raw).expanduser().absolute()


def _intake_entry(path: Path, info: os.stat_result) -> dict[str, Any]:
    return {
        "path": str(path),
        "resolved_path": str(path.resolve()),
        "size_bytes": info.st_size if stat.S_ISREG(info.st_mode) else 0,
        "mtime_ns": info.st_mtime_ns,
        "device": info.st_dev,
        "inode": info.st_ino,
        "kind": "directory" if stat.S_ISDIR(info.st_mode) else "file",
    }


def normalize_io_request_paths(request: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize local selections without importing scientific or GUI libraries."""
    paths = request.get("paths")
    if not isinstance(paths, list) or not paths:
        raise ValueError("Select at least one source")
    return {**request, "paths": [str(_intake_path(path)) for path in paths]}


def inspection_sha256(inspection: Mapping[str, Any]) -> str:
    """Digest the entire confirmation, including ordered inputs and native version."""
    return hashlib.sha256(
        json.dumps(
            {
                key: value
                for key, value in inspection.items()
                if key != "inspection_sha256"
            },
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def compact_inspection(inspection: dict[str, Any]) -> dict[str, Any]:
    """Send bounded summaries and defaults while retaining request data locally."""
    summary = {
        key: value
        for key, value in inspection.items()
        if key not in ("request", "paths", "inspection_sha256")
    }
    summary["request_defaults"] = {
        key: inspection["request"][key]
        for key in ("datatype", "format", "combine", "max_bytes", "max_entries")
    }
    return {
        "io_inspection": summary,
        "inspection_sha256": inspection_sha256(inspection),
    }


def restore_inspection(
    response: Mapping[str, Any], request: Mapping[str, Any]
) -> dict[str, Any]:
    """Restore the unchanged local/GUI structure from a compact worker response."""
    summary = dict(response["io_inspection"])
    defaults = summary.pop("request_defaults", None)
    if defaults is None:
        return summary  # Legacy small responses remain supported.
    normalized = {
        **defaults,
        "paths": list(request["paths"]),
        "args": request.get("args", []),
        "kwargs": request.get("kwargs", {}),
    }
    restored = {**summary, "request": normalized, "paths": normalized["paths"]}
    if inspection_sha256(restored) != response.get("inspection_sha256"):
        raise ValueError("Inspection transport digest does not match normalized inputs")
    restored["inspection_sha256"] = response["inspection_sha256"]
    return restored


def _intake_preview(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    preview = []
    size = 2
    for entry in entries[:_INSPECTION_PREVIEW_ENTRIES]:
        size += len(json.dumps(entry, separators=(",", ":")).encode("utf-8")) + 1
        if size > _INSPECTION_PREVIEW_BYTES:
            break
        preview.append(entry)
    return preview


def inspect_io(request: dict[str, Any]) -> dict[str, Any]:
    """Inspect selected files/directories without invoking a scientific reader."""
    datatype = request.get("datatype", "TimeSeries")
    io_class(datatype)
    raw_paths = request.get("paths")
    if not isinstance(raw_paths, list) or not raw_paths:
        raise ValueError("Select at least one source")
    paths = [_intake_path(raw) for raw in raw_paths]
    max_bytes = _intake_limit(request.get("max_bytes", DEFAULT_IO_BYTES), "byte")
    max_entries = _intake_limit(request.get("max_entries", DEFAULT_IO_ENTRIES), "entry")
    if len(paths) > max_entries:
        raise ValueError("Selected source count exceeds entry limit")
    combine = request.get("combine", "individual")
    if combine not in ("individual", "combined"):
        raise ValueError("Choose individual or combined input")
    args, kwargs = request.get("args", []), request.get("kwargs", {})
    if not isinstance(args, list) or not isinstance(kwargs, dict):
        raise ValueError("I/O args must be a list and kwargs a mapping")
    decode_value(args)
    decode_value(kwargs)
    records: list[dict[str, Any]] = []
    roots: list[dict[str, Any]] = []
    total_bytes = 0
    for root in paths:
        stack: list[tuple[Path, frozenset[tuple[int, int]], int, bool]] = [
            (root, frozenset(), 0, True)
        ]
        while stack:
            path, ancestors, depth, is_root = stack.pop()
            if depth > _MAX_IO_DEPTH:
                raise ValueError("Source directory nesting exceeds depth limit")
            info = path.stat()
            identity = (info.st_dev, info.st_ino)
            if not stat.S_ISREG(info.st_mode) and not stat.S_ISDIR(info.st_mode):
                raise ValueError(f"Source is not a regular file or directory: {path}")
            entry = _intake_entry(path, info)
            if is_root:
                roots.append(entry)
            if not is_root or entry["kind"] == "file":
                records.append(entry)
                if len(records) > max_entries:
                    raise ValueError(
                        f"Source entry count exceeds entry limit {max_entries}"
                    )
            if entry["kind"] == "file":
                total_bytes += entry["size_bytes"]
                if total_bytes > max_bytes:
                    raise ValueError(
                        f"Source byte count exceeds byte limit {max_bytes}"
                    )
            else:
                if identity in ancestors:
                    raise ValueError(f"Source directory cycle detected: {path}")
                next_ancestors = ancestors | {identity}
                with os.scandir(path) as children:
                    for child in children:
                        if len(records) + len(stack) >= max_entries:
                            raise ValueError(
                                f"Source entry count exceeds entry limit {max_entries}"
                            )
                        stack.append(
                            (Path(child.path), next_ancestors, depth + 1, False)
                        )
    fmt = request.get("format") or None
    if fmt is not None and (
        not isinstance(fmt, str) or not fmt.strip() or len(fmt) > 256
    ):
        raise ValueError("Format must be a nonempty native format name")
    if fmt is None:
        guesses = {
            identify_io(datatype, str(path), args=args, kwargs=kwargs) for path in paths
        }
        if len(guesses) != 1:
            raise ValueError("Select a common format explicitly for these sources")
        fmt = guesses.pop()
    normalized = {
        "datatype": datatype,
        "format": fmt,
        "paths": [str(p) for p in paths],
        "args": args,
        "kwargs": kwargs,
        "combine": combine,
        "max_bytes": max_bytes,
        "max_entries": max_entries,
    }
    ordered_records = sorted(records, key=lambda item: item["path"])
    identity_digest = hashlib.sha256(
        json.dumps(
            {"entries": ordered_records, "roots": roots},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    entries_preview = _intake_preview(ordered_records)
    roots_preview = _intake_preview(roots)
    inspection = {
        "request": normalized,
        "gwexpy_version": version("gwexpy"),
        "entries": entries_preview,
        "roots": roots_preview,
        "inventory_sha256": identity_digest,
        "entries_previewed": len(entries_preview),
        "roots_previewed": len(roots_preview),
        "total_bytes": total_bytes,
        "entry_count": len(records),
        "datatype": datatype,
        "format": fmt,
        "paths": normalized["paths"],
    }
    inspection["inspection_sha256"] = inspection_sha256(inspection)
    return inspection


def validate_inspection(inspection: dict[str, Any]) -> None:
    """Require the selected source identities to match the confirmed inspection."""
    current = inspect_io(inspection["request"])
    if current != inspection:
        raise ValueError(
            "Selected source changed after inspection; inspect and confirm again"
        )
