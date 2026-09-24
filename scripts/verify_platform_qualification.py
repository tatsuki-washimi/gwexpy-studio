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
    from .trial_targets import TrialTargetError, trial_target
except ImportError:  # pragma: no cover - direct CLI invocation.
    from trial_targets import (  # type: ignore[no-redef]
        TrialTargetError,
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
_LEGACY_CAMPAIGN_TARGET_IDS = frozenset({"macos15-arm64", "wsl2-ubuntu24"})

# Schema 2 is deliberately a separate reader and writer.  The legacy
# summaries below continue to consume only schema 1 documents.
_SCHEMA2_RESULT_FIELDS = {
    "architecture",
    "automated_checks",
    "build_id",
    "cleanup",
    "error_code",
    "host",
    "kit_manifest_sha256",
    "owner_confirmations",
    "schema",
    "source_sha",
    "stage",
    "status",
    "target_id",
    "wheel_sha256",
    "zip_sha256",
}
_SCHEMA2_STAGES = frozenset(
    {
        "preflight",
        "provenance",
        "preconditions",
        "environment",
        "automatic",
        "owner",
        "cleanup",
        "result-save",
        "complete",
    }
)
_SCHEMA2_ERRORS = frozenset(
    {
        "checksum-mismatch",
        "source-mismatch",
        "kit-mismatch",
        "target-mismatch",
        "host-mismatch",
        "precondition-failed",
        "conda-unavailable",
        "environment-failed",
        "import-isolation-failed",
        "automatic-failed",
        "owner-false",
        "owner-unanswered",
        "owner-cancelled",
        "interrupted",
        "cleanup-failed",
        "result-save-failed",
        "runner-failed",
    }
)
_SCHEMA2_COMMON_AUTOMATED = frozenset(
    {
        "archive_checksum",
        "source_manifest",
        "kit_manifest",
        "wheel_identity",
        "conda_runtime",
        "replay_runtime",
        "import_isolation",
        "technical_gate",
        "qt_backend",
        # These checks are retained from the physical probe contract.  They
        # are automatic evidence; owner confirmations below remain separate.
        "about_build_id",
        "clipboard",
        "dpi_scaling",
        "qt_opengl",
        "qt_screen",
        "diagnostics_copy",
        "file_dialog_path",
    }
)
_SCHEMA2_TARGET_AUTOMATED = {
    "ubuntu24-x86_64": frozenset({"linux_unicode_space_path"}),
    "debian13-x86_64": frozenset({"linux_unicode_space_path"}),
    "wsl2-ubuntu24": frozenset(
        {
            "guest_architecture",
            "windows_architecture",
            "wsl2_kernel",
            "wslg_display",
            "qt_egl_gl",
            "windows_clipboard_roundtrip",
            "windows_unicode_space_path",
        }
    ),
    "macos15-arm64": frozenset(
        {
            "cocoa_platform",
            "macos_version",
            "native_arm64",
            "macos_unicode_space_path",
        }
    ),
}
_SCHEMA2_OWNER = (
    "native_file_dialog",
    "normal_scale_display",
    "normal_scale_operation",
    "alternate_scale_display",
    "alternate_scale_operation",
)
_SCHEMA2_HOST_FIELDS = {
    "ubuntu24-x86_64": frozenset(
        {"architecture", "host_os", "os_version", "python_version", "qt_backend"}
    ),
    "debian13-x86_64": frozenset(
        {"architecture", "host_os", "os_version", "python_version", "qt_backend"}
    ),
    "wsl2-ubuntu24": frozenset(
        {
            "guest_architecture",
            "guest_os",
            "guest_version",
            "kernel_release",
            "python_version",
            "qt_backend",
            "windows_architecture",
            "windows_version",
        }
    ),
    "macos15-arm64": frozenset(
        {"architecture", "macos_version", "python_version", "qt_backend"}
    ),
}
_SCHEMA2_VERSION = re.compile(r"[0-9]+(?:\.[0-9]+){0,3}")
_SCHEMA2_ARCHITECTURES = {
    "ubuntu24-x86_64": "x86_64",
    "debian13-x86_64": "x86_64",
    "wsl2-ubuntu24": None,
    "macos15-arm64": "arm64",
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


def required_schema2_automated_checks(target_id: str) -> tuple[str, ...]:
    """Return the fixed schema-2 automatic check names for one target."""
    try:
        trial_target(target_id)
    except TrialTargetError as error:
        raise QualificationError(str(error)) from error
    return tuple(
        sorted(_SCHEMA2_COMMON_AUTOMATED | _SCHEMA2_TARGET_AUTOMATED[target_id])
    )


def required_schema2_owner_confirmations(target_id: str) -> tuple[str, ...]:
    """Return the fixed, bounded owner confirmation names."""
    try:
        trial_target(target_id)
    except TrialTargetError as error:
        raise QualificationError(str(error)) from error
    return _SCHEMA2_OWNER


def _schema2_sha(value: object, label: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise QualificationError(f"invalid {label}")
    return value


def _schema2_build(value: object) -> str:
    if not isinstance(value, str) or _BUILD_ID.fullmatch(value) is None:
        raise QualificationError("invalid Build ID")
    return value


def _schema2_host(
    target_id: str, architecture: str, host: Mapping[str, object]
) -> dict[str, str]:
    """Validate only bounded native host facts used by schema 2."""
    fields = _SCHEMA2_HOST_FIELDS[target_id]
    if set(host) != fields or not all(isinstance(item, str) for item in host.values()):
        raise QualificationError("schema 2 host fields are invalid")
    result = cast(dict[str, str], dict(host))
    for name in ("os_version", "macos_version", "windows_version"):
        if name in result:
            _schema2_version(result[name], name)
    if not result.get("python_version", "").startswith("3.12."):
        raise QualificationError("schema 2 requires CPython 3.12")
    if target_id in {"ubuntu24-x86_64", "debian13-x86_64"}:
        expected_os = "ubuntu" if target_id.startswith("ubuntu") else "debian"
        expected_version = "24.04" if expected_os == "ubuntu" else "13"
        if (
            result["architecture"] != architecture
            or result["architecture"] != "x86_64"
            or result["host_os"] != expected_os
            or result["os_version"] != expected_version
            or not result["python_version"].startswith("3.12.")
        ):
            raise QualificationError("native host identity does not match target")
    elif target_id == "wsl2-ubuntu24":
        if (
            architecture not in {"x86_64", "aarch64"}
            or result["guest_architecture"] != architecture
            or result["windows_architecture"] != architecture
            or result["guest_os"] != "ubuntu"
            or result["guest_version"] != "24.04"
            or _WSL_KERNEL.fullmatch(result["kernel_release"]) is None
            or int(result["windows_version"].split(".", 1)[0]) < 11
            or not result["python_version"].startswith("3.12.")
        ):
            raise QualificationError("WSL2 host identity does not match target")
    else:
        if (
            architecture != "arm64"
            or result["architecture"] != "arm64"
            or not result["macos_version"].split(".", 1)[0].isdigit()
            or int(result["macos_version"].split(".", 1)[0]) < 15
        ):
            raise QualificationError("native macOS host identity does not match target")
    _schema2_version(
        result["python_version"], "Python version"
    ) if "python_version" in result else None
    if result["qt_backend"] not in trial_target(target_id).allowed_backends:
        raise QualificationError("Qt backend is outside target allowance")
    return result


def _schema2_version(value: object, label: str) -> str:
    if not isinstance(value, str) or _SCHEMA2_VERSION.fullmatch(value) is None:
        raise QualificationError(f"invalid {label}")
    return value


def qualification2_result_bytes(
    *,
    target_id: str,
    architecture: str,
    build_id: str,
    source_sha: str,
    wheel_sha256: str,
    zip_sha256: str,
    kit_manifest_sha256: str,
    host: Mapping[str, object],
    automated_checks: Mapping[str, object],
    owner_confirmations: Mapping[str, object] | None = None,
    cleanup: bool | None = None,
    stage: str = "preflight",
    error_code: str | None = None,
) -> bytes:
    """Serialize schema-2 evidence with explicit unexecuted ``null`` values."""
    try:
        target = trial_target(target_id)
        target.require_architecture(architecture)
    except TrialTargetError as error:
        raise QualificationError(str(error)) from error
    build = _schema2_build(build_id)
    source = _SOURCE_SHA.fullmatch(source_sha) if isinstance(source_sha, str) else None
    if source is None or source.group(0) != source_sha:
        raise QualificationError("invalid source SHA")
    if build[2:9] != source_sha[:7]:
        raise QualificationError("Build ID does not match source SHA")
    wheel_hash = _schema2_sha(wheel_sha256, "wheel SHA-256")
    zip_hash = _schema2_sha(zip_sha256, "ZIP SHA-256")
    kit_hash = _schema2_sha(kit_manifest_sha256, "kit manifest SHA-256")
    if stage not in _SCHEMA2_STAGES:
        raise QualificationError("invalid schema 2 stage")
    if error_code is not None and error_code not in _SCHEMA2_ERRORS:
        raise QualificationError("invalid schema 2 error code")
    expected_automated = set(required_schema2_automated_checks(target_id))
    expected_owner = set(required_schema2_owner_confirmations(target_id))
    if set(automated_checks) != expected_automated:
        raise QualificationError("schema 2 automated checks do not match target")
    if not all(
        value is None or type(value) is bool for value in automated_checks.values()
    ):
        raise QualificationError("schema 2 automated checks must be boolean or null")
    owners = (
        {name: None for name in expected_owner}
        if owner_confirmations is None
        else dict(owner_confirmations)
    )
    if set(owners) != expected_owner or not all(
        value is None or type(value) is bool for value in owners.values()
    ):
        raise QualificationError("schema 2 owner confirmations do not match target")
    if cleanup is not None and type(cleanup) is not bool:
        raise QualificationError("schema 2 cleanup must be boolean or null")
    validated_host = _schema2_host(target_id, architecture, host)
    all_automatic = all(value is True for value in automated_checks.values())
    all_owner = all(value is True for value in owners.values())
    passed = (
        all_automatic
        and all_owner
        and cleanup is True
        and error_code is None
        and stage == "complete"
    )
    status = "passed" if passed else "failed"
    return _canonical_json(
        {
            "architecture": architecture,
            "automated_checks": dict(automated_checks),
            "build_id": build,
            "cleanup": cleanup,
            "error_code": error_code,
            "host": validated_host,
            "kit_manifest_sha256": kit_hash,
            "owner_confirmations": owners,
            "schema": 2,
            "source_sha": source_sha,
            "stage": stage,
            "status": status,
            "target_id": target_id,
            "wheel_sha256": wheel_hash,
            "zip_sha256": zip_hash,
        }
    )


def read_qualification2_result(
    raw: bytes,
    *,
    expected_identity: Mapping[str, object] | None = None,
    require_passed: bool = False,
) -> dict[str, object]:
    """Read schema 2 without allowing schema 1 evidence into a new gate."""
    try:
        document = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise QualificationError("qualification result is not valid JSON") from error
    if not isinstance(document, dict) or set(document) != _SCHEMA2_RESULT_FIELDS:
        raise QualificationError("qualification result fields do not match schema 2")
    if _canonical_json(document) != raw or document.get("schema") != 2:
        raise QualificationError("qualification result is not canonical schema 2 JSON")
    target_id = document.get("target_id")
    architecture = document.get("architecture")
    identity_names = (
        "target_id",
        "architecture",
        "build_id",
        "source_sha",
        "wheel_sha256",
        "zip_sha256",
        "kit_manifest_sha256",
    )
    if all(document.get(name) is None for name in identity_names):
        if (
            document["status"] != "failed"
            or document["host"] != {}
            or document["automated_checks"] != {}
            or document["owner_confirmations"] != {}
            or document["stage"] not in _SCHEMA2_STAGES
            or document["error_code"] not in _SCHEMA2_ERRORS
            or (
                document["cleanup"] is not None
                and type(document["cleanup"]) is not bool
            )
        ):
            raise QualificationError("untrusted schema 2 diagnostic is invalid")
        if require_passed:
            raise QualificationError("untrusted schema 2 diagnostic cannot pass")
        return cast(dict[str, object], document)
    if not isinstance(target_id, str) or not isinstance(architecture, str):
        raise QualificationError("schema 2 target or architecture is invalid")
    automated = document.get("automated_checks")
    owners = document.get("owner_confirmations")
    host = document.get("host")
    if (
        not isinstance(automated, dict)
        or not isinstance(owners, dict)
        or not isinstance(host, dict)
    ):
        raise QualificationError("schema 2 result sections are invalid")
    rebuilt = qualification2_result_bytes(
        target_id=target_id,
        architecture=architecture,
        build_id=cast(str, document["build_id"]),
        source_sha=cast(str, document["source_sha"]),
        wheel_sha256=cast(str, document["wheel_sha256"]),
        zip_sha256=cast(str, document["zip_sha256"]),
        kit_manifest_sha256=cast(str, document["kit_manifest_sha256"]),
        host=host,
        automated_checks=automated,
        owner_confirmations=owners,
        cleanup=document["cleanup"]
        if document["cleanup"] is None
        else cast(bool, document["cleanup"]),
        stage=cast(str, document["stage"]),
        error_code=document["error_code"]
        if document["error_code"] is None
        else cast(str, document["error_code"]),
    )
    if rebuilt != raw:
        raise QualificationError("schema 2 result does not match derived status")
    if expected_identity is not None:
        for field in (
            "target_id",
            "architecture",
            "build_id",
            "source_sha",
            "wheel_sha256",
            "zip_sha256",
            "kit_manifest_sha256",
        ):
            if (
                field in expected_identity
                and document[field] != expected_identity[field]
            ):
                raise QualificationError(
                    "schema 2 result identity does not match expected values"
                )
    if require_passed and document["status"] != "passed":
        raise QualificationError("schema 2 result is not a pass")
    return cast(dict[str, object], document)


def qualification2_untrusted_failure_bytes(
    *, stage: str, error_code: str, cleanup: bool | None = None
) -> bytes:
    """Serialize a pre-provenance diagnostic without asserting identity.

    The all-null identity is intentional.  A checksum, target, or host failure
    before release verification cannot be reported as evidence for any source.
    """
    if stage not in _SCHEMA2_STAGES or error_code not in _SCHEMA2_ERRORS:
        raise QualificationError("invalid untrusted schema 2 diagnostic")
    if cleanup is not None and type(cleanup) is not bool:
        raise QualificationError("schema 2 cleanup must be boolean or null")
    return _canonical_json(
        {
            "architecture": None,
            "automated_checks": {},
            "build_id": None,
            "cleanup": cleanup,
            "error_code": error_code,
            "host": {},
            "kit_manifest_sha256": None,
            "owner_confirmations": {},
            "schema": 2,
            "source_sha": None,
            "stage": stage,
            "status": "failed",
            "target_id": None,
            "wheel_sha256": None,
            "zip_sha256": None,
        }
    )


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
    """Bind the historical WSL2-plus-macOS campaign into one summary.

    The campaign summary predates the four-target qualification contract.  Keep
    its explicit three-configuration input set stable; the later group and
    five-target audit summaries own the expanded target coverage.
    """
    if not results:
        raise QualificationError("no qualification results supplied")
    grouped: dict[str, list[bytes]] = {}
    source_shas: set[str] = set()
    for raw in results:
        document = _read_result(raw)
        target_id = cast(str, document["target_id"])
        grouped.setdefault(target_id, []).append(raw)
        source_shas.add(cast(str, document["source_sha"]))
    if set(grouped) != _LEGACY_CAMPAIGN_TARGET_IDS:
        raise QualificationError("legacy campaign target set is incomplete")
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
