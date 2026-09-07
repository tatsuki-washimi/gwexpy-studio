"""Privacy-preserving facts for the Studio ``Copy Diagnostics`` action."""

from __future__ import annotations

import json
import os
import platform
import re
from importlib.metadata import PackageNotFoundError, version

from .._version import __version__

UNKNOWN = "unknown"
_MAX_DIAGNOSTIC_VERSION = 1_000_000
_BUILD_ID = re.compile(
    r"P-[0-9a-f]{7,40}-[0-9]{8}(?:-[0-9]{2}|-r[1-9][0-9]*-a[1-9][0-9]*)"
)
_SOURCE_COMMIT = re.compile(r"[0-9a-f]{40}")
_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,63}")
_SHA256 = re.compile(r"[0-9a-fA-F]{64}")
_DISPLAY = re.compile(r"[A-Za-z0-9._+ -]{1,128}")
_MAX_EFFECTIVE_SNAPSHOT_DIAGNOSTIC_CHARS = 16 * 1024

# Keep this explicit rather than accepting arbitrary token-shaped text.  Error
# codes can cross the worker boundary, while filenames and recovery labels can
# have the same shape.  Adding a new code is a deliberate diagnostics contract
# change with a corresponding test.
_STUDIO_ERROR_CODES = frozenset(
    {
        "bridge_busy",
        "bridge_unavailable",
        "command_pending",
        "data_write_failed",
        "deadline_propagation_failed",
        "export_unavailable",
        "hdf5_schema_guard_unavailable",
        "inspection_identity_unavailable",
        "invalid_command",
        "invalid_drop",
        "invalid_input_kind",
        "invalid_manifest",
        "invalid_operation",
        "invalid_params",
        "invalid_payload",
        "invalid_response",
        "invalid_source_format",
        "invalid_ui_state",
        "io_capability_unavailable",
        "malformed_message",
        "materialization_failed",
        "modal_active",
        "no_replay_target",
        "object_not_found",
        "operation_cancelled",
        "operation_failed",
        "operation_unavailable",
        "overwrite_confirmation_required",
        "payload_too_large",
        "preview_release_failed",
        "project_path_required",
        "project_read_only",
        "protocol_mismatch",
        "probe_failed",
        "recovery_unavailable",
        "recent_projects_unavailable",
        "redo_unavailable",
        "registry_missing",
        "request_id_mismatch",
        "restore_confirmation_required",
        "restore_incomplete",
        "restore_required",
        "sample_unavailable",
        "shm_invalid_descriptor",
        "shm_not_found",
        "source_changed",
        "source_not_found",
        "source_too_large",
        "timeout",
        "undo_unavailable",
        "unreviewed_registry_entry",
        "unknown_message_type",
        "unsupported_source_format",
        "workspace_failed",
        "worker_already_started",
        "worker_busy",
        "worker_crashed",
        "worker_timeout",
        "worker_unavailable",
        "runtime_dependency_missing",
    }
)

_DIAGNOSTIC_FIELDS = (
    "studio_version",
    "build_id",
    "source_commit",
    "os",
    "platform",
    "python",
    "pyside6",
    "gwexpy",
    "gwpy",
    "project_schema_version",
    "worker_protocol_version",
    "io_capability_digest",
    "io_capability_snapshot",
    "last_error_code",
)
_DISPLAY_LABELS = {
    "studio_version": "Studio",
    "build_id": "Build",
    "source_commit": "Source commit",
    "os": "OS",
    "platform": "Platform",
    "python": "Python",
    "pyside6": "Qt/PySide",
    "gwexpy": "GWexpy",
    "gwpy": "GWpy",
    "project_schema_version": "Project schema",
    "worker_protocol_version": "Worker protocol",
    "io_capability_digest": "I/O capability digest",
    "io_capability_snapshot": "I/O capability snapshot",
    "last_error_code": "Last error code",
}


def _safe_match(value: object, expression: re.Pattern[str]) -> str:
    if type(value) is str and expression.fullmatch(value):
        return value
    return UNKNOWN


def _safe_nonnegative_integer(value: object) -> str:
    if type(value) is int and 0 <= value <= _MAX_DIAGNOSTIC_VERSION:
        return str(value)
    return UNKNOWN


def _safe_display(value: object) -> str:
    if type(value) is str and _DISPLAY.fullmatch(value):
        return value
    return UNKNOWN


def _safe_error_code(value: object) -> str:
    if type(value) is str and value in _STUDIO_ERROR_CODES:
        return value
    return UNKNOWN


def _safe_effective_capability_snapshot(value: object) -> str:
    """Summarize a worker snapshot without echoing worker-controlled text.

    The worker response is an untrusted boundary.  Even a structurally valid
    snapshot can contain a token-shaped format or a Tier B caveat, so Copy
    Diagnostics retains only fixed-enum counts plus the independently checked
    snapshot and policy digests.
    """
    try:
        from ..ops.io_capabilities import EffectiveCapabilitySnapshot

        snapshot = EffectiveCapabilitySnapshot.from_document(value)
        status_counts = {
            status: sum(entry.status == status for entry in snapshot.entries)
            for status in ("verified", "experimental", "unavailable")
        }
        reason_counts: dict[str, int] = {}
        for entry in snapshot.entries:
            if entry.reason is not None:
                reason_counts[entry.reason] = reason_counts.get(entry.reason, 0) + 1
        serialized = json.dumps(
            {
                "schema_version": 1,
                "mode": snapshot.mode,
                "policy_digest": snapshot.policy_digest,
                "digest": snapshot.digest,
                "entry_count": len(snapshot.entries),
                "status_counts": status_counts,
                "unavailable_reason_counts": reason_counts,
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
    except (TypeError, ValueError, OverflowError):
        return UNKNOWN
    return (
        serialized
        if len(serialized) <= _MAX_EFFECTIVE_SNAPSHOT_DIAGNOSTIC_CHARS
        else UNKNOWN
    )


def _distribution_version(*names: str) -> str:
    for name in names:
        try:
            candidate = version(name)
        except (PackageNotFoundError, OSError, ValueError):
            # Diagnostics are best-effort; a malformed or unreadable package
            # metadata record is equivalent to an unavailable component.
            continue
        safe = _safe_match(candidate, _VERSION)
        if safe != UNKNOWN:
            return safe
    return UNKNOWN


def _platform_summary() -> str:
    summary = " ".join(
        value
        for value in (platform.system(), platform.release(), platform.machine())
        if value
    )
    return _safe_display(summary)


def diagnostics_payload(
    *,
    project_schema_version: object = None,
    worker_protocol_version: object = None,
    io_capability_digest: object = None,
    io_capability_snapshot: object = None,
    last_error_code: object = None,
) -> dict[str, str]:
    """Return a fixed, allowlisted diagnostics payload without user paths.

    Values accepted from the application are constrained to integers, a digest,
    and a machine error code.  Unknown or unsafe values are represented by the
    literal ``unknown`` instead of echoing user-controlled content.
    """
    return {
        "studio_version": _safe_match(__version__, _VERSION),
        "build_id": _safe_match(os.environ.get("GWEXPY_STUDIO_BUILD_ID"), _BUILD_ID),
        "source_commit": _safe_match(
            os.environ.get("GWEXPY_STUDIO_SOURCE_COMMIT"), _SOURCE_COMMIT
        ),
        "os": _safe_display(platform.system()),
        "platform": _platform_summary(),
        "python": _safe_match(platform.python_version(), _VERSION),
        "pyside6": _distribution_version("PySide6-Essentials", "PySide6"),
        "gwexpy": _distribution_version("gwexpy"),
        "gwpy": _distribution_version("gwpy"),
        "project_schema_version": _safe_nonnegative_integer(project_schema_version),
        "worker_protocol_version": _safe_nonnegative_integer(worker_protocol_version),
        "io_capability_digest": _safe_match(io_capability_digest, _SHA256).lower(),
        "io_capability_snapshot": _safe_effective_capability_snapshot(
            io_capability_snapshot
        ),
        "last_error_code": _safe_error_code(last_error_code),
    }


def format_diagnostics(
    *,
    project_schema_version: object = None,
    worker_protocol_version: object = None,
    io_capability_digest: object = None,
    io_capability_snapshot: object = None,
    last_error_code: object = None,
) -> str:
    """Format only allowlisted, sanitized fields for clipboard sharing.

    This is intentionally not a log export: paths, project names, recovery
    information, exception text, and arbitrary mapping values are never copied.
    """
    values = diagnostics_payload(
        project_schema_version=project_schema_version,
        worker_protocol_version=worker_protocol_version,
        io_capability_digest=io_capability_digest,
        io_capability_snapshot=io_capability_snapshot,
        last_error_code=last_error_code,
    )
    return "\n".join(
        f"{_DISPLAY_LABELS[field]}: {values[field]}" for field in _DIAGNOSTIC_FIELDS
    )
