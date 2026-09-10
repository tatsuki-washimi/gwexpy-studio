"""Create and verify privacy-bounded physical qualification evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import cast

try:  # Support imports and direct ``python scripts/...`` execution.
    from .trial_targets import TrialTargetError, target_ids, trial_target
except ImportError:  # pragma: no cover - direct CLI invocation.
    from trial_targets import (  # type: ignore[no-redef]
        TrialTargetError,
        target_ids,
        trial_target,
    )


class QualificationError(ValueError):
    """Raised when physical qualification evidence is incomplete or untrusted."""


_SOURCE_SHA = re.compile(r"[0-9a-f]{40}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_BUILD_ID = re.compile(r"P-(?P<sha>[0-9a-f]{7})-[0-9]{8}-r[1-9][0-9]*-a[1-9][0-9]*")
_VERSION = re.compile(r"[0-9]+(?:\.[0-9]+){1,3}")
_WSL_KERNEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]*[Mm]icrosoft[A-Za-z0-9._+-]*")
_RESULT_FIELDS = {
    "architecture",
    "build_id",
    "checks",
    "host",
    "schema",
    "source_sha",
    "status",
    "target_id",
    "wheel_sha256",
}
_COMMON_CHECKS = {
    "about_build_id",
    "archive_checksum",
    "asd",
    "binary_install",
    "bundle_verification",
    "clipboard",
    "close_project",
    "crop",
    "dpi_scaling",
    "environment_isolation",
    "import_origin",
    "launcher",
    "load",
    "project_reopen",
    "python_export",
    "qt_screen",
    "recovery",
    "save_project",
    "shared_memory_cleanup",
    "try_sample",
    "welcome",
    "worker_exit",
}
_TARGET_CHECKS = {
    "wsl2-ubuntu24": {
        "diagnostics_copy",
        "guest_architecture",
        "linux_unicode_space_path",
        "qt_egl_gl",
        "windows_architecture",
        "windows_clipboard_roundtrip",
        "windows_unicode_space_path",
        "wsl2_kernel",
        "wslg_display",
    },
    "macos15-arm64": {
        "cocoa_platform",
        "diagnostics_copy",
        "file_dialog_path",
        "macos_unicode_space_path",
        "macos_version",
        "native_arm64",
        "qt_opengl",
    },
}
_HOST_FIELDS = {
    "wsl2-ubuntu24": {
        "guest_architecture",
        "guest_os",
        "guest_version",
        "kernel_release",
        "python_version",
        "windows_architecture",
        "windows_version",
    },
    "macos15-arm64": {"architecture", "macos_version", "python_version"},
}


def _canonical_json(document: object) -> bytes:
    return (
        json.dumps(document, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
        + "\n"
    ).encode("utf-8")


def required_checks(target_id: str) -> tuple[str, ...]:
    """Return the complete canonical check set for a supported target."""
    try:
        trial_target(target_id)
        target_checks = _TARGET_CHECKS[target_id]
    except (KeyError, TrialTargetError) as error:
        raise QualificationError(str(error)) from error
    return tuple(sorted(_COMMON_CHECKS | target_checks))


def _require_identity(
    build_id: object, source_sha: object, wheel_sha256: object
) -> None:
    if (
        not isinstance(build_id, str)
        or (match := _BUILD_ID.fullmatch(build_id)) is None
    ):
        raise QualificationError("invalid Build ID")
    if not isinstance(source_sha, str) or _SOURCE_SHA.fullmatch(source_sha) is None:
        raise QualificationError("invalid source SHA")
    if match.group("sha") != source_sha[:7]:
        raise QualificationError("Build ID does not match source SHA")
    if not isinstance(wheel_sha256, str) or _SHA256.fullmatch(wheel_sha256) is None:
        raise QualificationError("invalid wheel SHA-256")


def _require_version(
    value: object, label: str, minimum_major: int | None = None
) -> str:
    if not isinstance(value, str) or _VERSION.fullmatch(value) is None:
        raise QualificationError(f"invalid {label}")
    if minimum_major is not None and int(value.split(".", 1)[0]) < minimum_major:
        raise QualificationError(f"unsupported {label}")
    return value


def _validated_host(
    target_id: str, architecture: str, host: Mapping[str, object]
) -> dict[str, str]:
    expected_fields = _HOST_FIELDS[target_id]
    if set(host) != expected_fields:
        raise QualificationError(f"invalid {target_id} host fields")
    if not all(isinstance(value, str) for value in host.values()):
        raise QualificationError("host evidence values must be strings")
    result = cast(dict[str, str], dict(host))
    _require_version(result["python_version"], "Python version")
    if not result["python_version"].startswith("3.12."):
        raise QualificationError("qualification requires Python 3.12")

    if target_id == "wsl2-ubuntu24":
        if result["guest_architecture"] != architecture:
            raise QualificationError("guest architecture does not match result")
        if result["windows_architecture"] != architecture:
            raise QualificationError("Windows architecture does not match result")
        if result["guest_os"] != "ubuntu" or result["guest_version"] != "24.04":
            raise QualificationError("qualification requires Ubuntu 24.04 guest")
        _require_version(result["windows_version"], "Windows version", 11)
        if _WSL_KERNEL.fullmatch(result["kernel_release"]) is None:
            raise QualificationError("kernel evidence is not WSL2")
    else:
        if architecture != "arm64" or result["architecture"] != "arm64":
            raise QualificationError("macOS qualification must be native arm64")
        _require_version(result["macos_version"], "macOS version", 15)
    return result


def qualification_result_bytes(
    *,
    target_id: str,
    architecture: str,
    build_id: str,
    source_sha: str,
    wheel_sha256: str,
    host: Mapping[str, object],
    checks: Mapping[str, object],
) -> bytes:
    """Return one canonical, path-free physical qualification result."""
    try:
        target = trial_target(target_id)
        target.require_architecture(architecture)
    except TrialTargetError as error:
        raise QualificationError(str(error)) from error
    _require_identity(build_id, source_sha, wheel_sha256)
    expected_checks = set(required_checks(target_id))
    if set(checks) != expected_checks:
        raise QualificationError("qualification check names do not match target")
    if not all(type(value) is bool for value in checks.values()):
        raise QualificationError("qualification checks must be boolean")
    status = (
        "passed" if all(cast(bool, value) for value in checks.values()) else "failed"
    )
    document = {
        "architecture": architecture,
        "build_id": build_id,
        "checks": dict(checks),
        "host": _validated_host(target_id, architecture, host),
        "schema": 1,
        "source_sha": source_sha,
        "status": status,
        "target_id": target_id,
        "wheel_sha256": wheel_sha256,
    }
    return _canonical_json(document)


def _read_result(raw: bytes) -> dict[str, object]:
    try:
        document = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise QualificationError("qualification result is not valid JSON") from error
    if not isinstance(document, dict) or set(document) != _RESULT_FIELDS:
        raise QualificationError("qualification result fields do not match schema 1")
    if _canonical_json(document) != raw:
        raise QualificationError("qualification result is not canonical JSON")
    if document["schema"] != 1:
        raise QualificationError("unsupported qualification result schema")
    target_id = document["target_id"]
    architecture = document["architecture"]
    if not isinstance(target_id, str) or not isinstance(architecture, str):
        raise QualificationError("invalid target or architecture")
    host = document["host"]
    checks = document["checks"]
    if not isinstance(host, dict) or not isinstance(checks, dict):
        raise QualificationError("host and checks must be objects")
    rebuilt = qualification_result_bytes(
        target_id=target_id,
        architecture=architecture,
        build_id=cast(str, document["build_id"]),
        source_sha=cast(str, document["source_sha"]),
        wheel_sha256=cast(str, document["wheel_sha256"]),
        host=host,
        checks=checks,
    )
    if rebuilt != raw:
        raise QualificationError("qualification result does not match derived status")
    if document["status"] != "passed":
        raise QualificationError("qualification result contains a failed check")
    return cast(dict[str, object], document)


def qualification_summary_bytes(results: Sequence[bytes]) -> bytes:
    """Verify a target's complete native evidence and return its summary."""
    if not results:
        raise QualificationError("no qualification results supplied")
    parsed = [(raw, _read_result(raw)) for raw in results]
    first = parsed[0][1]
    identity_fields = ("target_id", "build_id", "source_sha", "wheel_sha256")
    if any(
        any(document[field] != first[field] for field in identity_fields)
        for _, document in parsed[1:]
    ):
        raise QualificationError("qualification result identities do not match")
    target_id = cast(str, first["target_id"])
    target = trial_target(target_id)
    architecture_rows: dict[str, dict[str, str]] = {}
    for raw, document in parsed:
        architecture = cast(str, document["architecture"])
        if architecture in architecture_rows:
            raise QualificationError("duplicate qualification architecture")
        architecture_rows[architecture] = {
            "architecture": architecture,
            "sha256": hashlib.sha256(raw).hexdigest(),
        }
    if set(architecture_rows) != set(target.architectures):
        raise QualificationError("qualification architecture set is incomplete")
    summary = {
        "build_id": first["build_id"],
        "results": [architecture_rows[name] for name in sorted(architecture_rows)],
        "schema": 1,
        "source_sha": first["source_sha"],
        "status": "passed",
        "target_id": target_id,
        "wheel_sha256": first["wheel_sha256"],
    }
    return _canonical_json(summary)


def qualification_campaign_summary_bytes(results: Sequence[bytes]) -> bytes:
    """Bind all required platform results from one source commit into one summary."""
    if not results:
        raise QualificationError("no qualification results supplied")
    grouped: dict[str, list[bytes]] = {}
    source_shas: set[str] = set()
    for raw in results:
        document = _read_result(raw)
        target_id = cast(str, document["target_id"])
        grouped.setdefault(target_id, []).append(raw)
        source_shas.add(cast(str, document["source_sha"]))
    if set(grouped) != set(target_ids()):
        raise QualificationError("qualification target set is incomplete")
    if len(source_shas) != 1:
        raise QualificationError("qualification results do not share one source SHA")

    target_rows: list[dict[str, object]] = []
    for target_id in sorted(grouped):
        target_summary = json.loads(qualification_summary_bytes(grouped[target_id]))
        target_rows.append(
            {
                "build_id": target_summary["build_id"],
                "results": target_summary["results"],
                "target_id": target_id,
                "wheel_sha256": target_summary["wheel_sha256"],
            }
        )
    return _canonical_json(
        {
            "schema": 1,
            "source_sha": next(iter(source_shas)),
            "status": "passed",
            "targets": target_rows,
        }
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Verify result files and write one canonical summary plus digest sidecar."""
    parser = argparse.ArgumentParser()
    parser.add_argument("results", nargs="+", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    arguments = parser.parse_args(argv)
    try:
        summary = qualification_campaign_summary_bytes(
            [path.read_bytes() for path in arguments.results]
        )
        arguments.output.write_bytes(summary)
        digest = hashlib.sha256(summary).hexdigest()
        arguments.output.with_suffix(arguments.output.suffix + ".sha256").write_text(
            f"{digest}  {arguments.output.name}\n", encoding="ascii"
        )
    except (OSError, QualificationError) as error:
        parser.error(str(error))
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI boundary.
    raise SystemExit(main())
