"""Strict, direction-level I/O contracts for frozen Studio artifacts.

The native GWexpy registry remains the source of truth for development.  A
frozen artifact opts into this policy only by setting
``GWEXPY_STUDIO_IO_CAPABILITIES`` to one regular manifest file.  Any problem
with that file deliberately becomes an empty, fail-closed capability set.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from ..errors import CodedStudioError

CAPABILITY_ENVIRONMENT = "GWEXPY_STUDIO_IO_CAPABILITIES"
CAPABILITY_SCHEMA_VERSION = 1
EFFECTIVE_CAPABILITY_SCHEMA_VERSION = 1
MAX_MANIFEST_BYTES = 1024 * 1024
# Each effective entry can approach 0.5 KiB after worker probe fields are
# added.  Keeping the snapshot at 1024 entries leaves roughly half of the
# 1 MiB control-message budget for the envelope and non-capability ping data.
MAX_EFFECTIVE_CAPABILITY_ENTRIES = 1024
MAX_MANIFEST_ENTRIES = MAX_EFFECTIVE_CAPABILITY_ENTRIES
KNOWN_IO_CLASSES = tuple(
    family + suffix
    for family in ("TimeSeries", "FrequencySeries", "Spectrogram")
    for suffix in ("", "Dict", "List", "Matrix")
)
TIER_C_REASONS = frozenset(
    {
        "backend_not_bundled",
        "fixture_unavailable",
        "native_unimplemented",
        "native_error",
        "frozen_unverified",
        "not_applicable",
    }
)
RUNTIME_UNAVAILABLE_REASONS = frozenset(
    {
        "runtime_dependency_missing",
        "probe_failed",
        "registry_missing",
        "unreviewed_registry_entry",
    }
)
UNAVAILABLE_REASONS = TIER_C_REASONS | RUNTIME_UNAVAILABLE_REASONS
_DIRECTIONS = frozenset({"read", "write"})
_IO_DIRECTIONS: tuple[Literal["read", "write"], ...] = ("read", "write")
_TIERS = frozenset({"A", "B", "C"})
_FORMAT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}")
_CAVEAT = re.compile(r"[A-Za-z][A-Za-z0-9 .,_()\-]{0,159}")
_SHA256 = re.compile(r"[0-9a-f]{64}")


class IOCapabilityError(CodedStudioError):
    """Raised before a frozen artifact can invoke an unavailable I/O direction."""

    default_code = "io_capability_unavailable"

    def __init__(self, *, reason: str = "frozen_unverified") -> None:
        """Keep a safe machine-readable Tier C reason without source details."""
        self.reason = reason if reason in UNAVAILABLE_REASONS else "frozen_unverified"
        super().__init__("I/O capability is unavailable in this artifact")


class _CapabilitySnapshotLimitError(RuntimeError):
    """Stop a registry scan before unreviewed entries can exhaust ping budget."""


@dataclass(frozen=True, slots=True)
class IOCapability:
    """One declared datatype, format, and direction capability."""

    datatype: str
    format: str
    direction: Literal["read", "write"]
    tier: Literal["A", "B", "C"]
    caveat: str | None = None
    reason: str | None = None

    def document(self) -> dict[str, str]:
        """Return the exact canonical-manifest shape for this capability."""
        value = {
            "datatype": self.datatype,
            "format": self.format,
            "direction": self.direction,
            "tier": self.tier,
        }
        if self.caveat is not None:
            value["caveat"] = self.caveat
        if self.reason is not None:
            value["reason"] = self.reason
        return value


@dataclass(frozen=True, slots=True)
class EffectiveIOCapability:
    """One worker-probed, presentation-safe I/O direction result.

    ``native_available`` records the registry fact independently from the
    release decision.  For example, an unreviewed native registry entry is
    present but remains unavailable in a frozen artifact.
    """

    datatype: str
    format: str
    direction: Literal["read", "write"]
    tier: Literal["A", "B", "C"]
    status: Literal["verified", "experimental", "unavailable"]
    native_available: bool
    auto_identify: bool
    caveat: str | None = None
    reason: str | None = None

    @property
    def available(self) -> bool:
        """Return whether this worker may invoke the native direction."""
        return self.status in {"verified", "experimental"}

    def document(self) -> dict[str, bool | str]:
        """Return the canonical, path-free worker-to-GUI representation."""
        value: dict[str, bool | str] = {
            "datatype": self.datatype,
            "format": self.format,
            "direction": self.direction,
            "tier": self.tier,
            "status": self.status,
            "native_available": self.native_available,
            "auto_identify": self.auto_identify,
        }
        if self.caveat is not None:
            value["caveat"] = self.caveat
        if self.reason is not None:
            value["reason"] = self.reason
        return value


def _effective_canonical_bytes(
    *,
    mode: Literal["developer", "frozen", "invalid"],
    policy_digest: str | None,
    entries: tuple[EffectiveIOCapability, ...],
) -> bytes:
    """Serialize worker-probed state without its self-referential digest."""
    document = {
        "schema_version": EFFECTIVE_CAPABILITY_SCHEMA_VERSION,
        "mode": mode,
        "policy_digest": policy_digest,
        "entries": [
            entry.document()
            for entry in sorted(
                entries,
                key=lambda item: (item.datatype, item.format, item.direction),
            )
        ],
    }
    return json.dumps(
        document,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")


@dataclass(frozen=True, slots=True)
class EffectiveCapabilitySnapshot:
    """Immutable intersection of reviewed policy and worker runtime facts."""

    mode: Literal["developer", "frozen", "invalid"]
    entries: tuple[EffectiveIOCapability, ...]
    policy_digest: str | None
    digest: str

    @property
    def active(self) -> bool:
        """Return whether this snapshot must restrict frozen-artifact I/O."""
        return self.mode != "developer"

    def capability(
        self, datatype: str, format_name: str, direction: str
    ) -> EffectiveIOCapability | None:
        """Find one direction, failing closed for active snapshot omissions."""
        if not self.active:
            return None
        for entry in self.entries:
            if (entry.datatype, entry.format, entry.direction) == (
                datatype,
                format_name,
                direction,
            ):
                return entry
        return EffectiveIOCapability(
            datatype=datatype,
            format=format_name,
            direction=_direction(direction),
            tier="C",
            status="unavailable",
            native_available=False,
            auto_identify=False,
            reason="frozen_unverified",
        )

    def require(
        self, datatype: str, format_name: str, direction: str
    ) -> EffectiveIOCapability | None:
        """Permit only a worker-probed available frozen-artifact direction."""
        entry = self.capability(datatype, format_name, direction)
        if entry is None or entry.available:
            return entry
        raise IOCapabilityError(reason=entry.reason or "frozen_unverified")

    def document(self) -> dict[str, object]:
        """Return the exact schema accepted by the untrusted GUI boundary."""
        return {
            "schema_version": EFFECTIVE_CAPABILITY_SCHEMA_VERSION,
            "mode": self.mode,
            "policy_digest": self.policy_digest,
            "entries": [entry.document() for entry in self.entries],
            "digest": self.digest,
        }

    @classmethod
    def from_document(cls, value: object) -> EffectiveCapabilitySnapshot:
        """Validate an untrusted worker snapshot without accepting diagnostics data."""
        if type(value) is not dict:
            raise ValueError("I/O capability snapshot must be an object")
        expected = {"schema_version", "mode", "policy_digest", "entries", "digest"}
        if set(value) != expected:
            raise ValueError("I/O capability snapshot schema is invalid")
        if (
            type(value["schema_version"]) is not int
            or value["schema_version"] != EFFECTIVE_CAPABILITY_SCHEMA_VERSION
        ):
            raise ValueError("I/O capability snapshot version is unsupported")
        mode = value["mode"]
        if mode not in {"developer", "frozen", "invalid"}:
            raise ValueError("I/O capability snapshot mode is invalid")
        policy_digest = value["policy_digest"]
        if policy_digest is not None and (
            type(policy_digest) is not str or _SHA256.fullmatch(policy_digest) is None
        ):
            raise ValueError("I/O capability snapshot policy digest is invalid")
        if (mode == "frozen") != (policy_digest is not None):
            raise ValueError("I/O capability snapshot policy digest is inconsistent")
        raw_entries = value["entries"]
        if type(raw_entries) is not list or len(raw_entries) > MAX_MANIFEST_ENTRIES:
            raise ValueError("I/O capability snapshot entries are invalid")
        if mode != "frozen" and raw_entries:
            raise ValueError("I/O capability snapshot mode cannot have entries")
        entries = tuple(_effective_entry(raw) for raw in raw_entries)
        keys = {(item.datatype, item.format, item.direction) for item in entries}
        if len(keys) != len(entries):
            raise ValueError("I/O capability snapshot has duplicate directions")
        digest = value["digest"]
        if type(digest) is not str or _SHA256.fullmatch(digest) is None:
            raise ValueError("I/O capability snapshot digest is invalid")
        canonical = _effective_canonical_bytes(
            mode=mode,
            policy_digest=policy_digest,
            entries=entries,
        )
        if hashlib.sha256(canonical).hexdigest() != digest:
            raise ValueError("I/O capability snapshot digest does not match content")
        return cls(
            mode=mode,
            entries=entries,
            policy_digest=policy_digest,
            digest=digest,
        )


@dataclass(frozen=True, slots=True)
class CapabilityManifest:
    """The active artifact policy, or an explicit development/fail-closed mode."""

    mode: Literal["developer", "frozen", "invalid"]
    entries: tuple[IOCapability, ...] = ()
    digest: str | None = None

    @property
    def active(self) -> bool:
        """Return whether this process must enforce an artifact policy."""
        return self.mode != "developer"

    def capability(
        self, datatype: str, format_name: str, direction: str
    ) -> IOCapability | None:
        """Find one direction, synthesizing Tier C for active omissions."""
        if not self.active:
            return None
        for entry in self.entries:
            if (entry.datatype, entry.format, entry.direction) == (
                datatype,
                format_name,
                direction,
            ):
                return entry
        return IOCapability(
            datatype=datatype,
            format=format_name,
            direction=_direction(direction),
            tier="C",
            reason="frozen_unverified",
        )

    def require(
        self, datatype: str, format_name: str, direction: str
    ) -> IOCapability | None:
        """Permit only Tier A/B directions when an artifact manifest is active."""
        entry = self.capability(datatype, format_name, direction)
        if entry is None or entry.tier in {"A", "B"}:
            return entry
        raise IOCapabilityError(reason=entry.reason or "frozen_unverified")


def validate_effective_capability_snapshot(
    snapshot: EffectiveCapabilitySnapshot, manifest: CapabilityManifest
) -> None:
    """Require a worker snapshot to be a non-escalating view of static policy."""
    if snapshot.mode != manifest.mode or snapshot.policy_digest != manifest.digest:
        raise ValueError("Worker capability policy does not match static policy")
    if snapshot.mode != "frozen":
        return
    policy = {
        (entry.datatype, entry.format, entry.direction): entry
        for entry in manifest.entries
    }
    effective = {
        (entry.datatype, entry.format, entry.direction): entry
        for entry in snapshot.entries
    }
    if set(policy) - set(effective):
        raise ValueError("Worker capability policy omits a static direction")
    runtime_reasons = RUNTIME_UNAVAILABLE_REASONS - {"unreviewed_registry_entry"}
    for key, entry in effective.items():
        static = policy.get(key)
        if static is None:
            if not (
                entry.tier == "C"
                and entry.status == "unavailable"
                and entry.reason == "unreviewed_registry_entry"
                and entry.native_available
            ):
                raise ValueError(
                    "Worker capability policy has an invalid external entry"
                )
            continue
        if entry.tier != static.tier or entry.caveat != static.caveat:
            raise ValueError("Worker capability policy changes a static direction")
        if static.tier == "C":
            if entry.status != "unavailable" or entry.reason != static.reason:
                raise ValueError(
                    "Worker capability policy escalates a Tier C direction"
                )
            continue
        available_status = "verified" if static.tier == "A" else "experimental"
        if entry.status == available_status:
            continue
        if entry.status != "unavailable" or entry.reason not in runtime_reasons:
            raise ValueError("Worker capability policy has an invalid runtime result")


def _direction(value: str) -> Literal["read", "write"]:
    """Validate a direction before using it in a capability key."""
    if value not in _DIRECTIONS:
        raise ValueError("I/O capability direction must be read or write")
    return value  # type: ignore[return-value]


def _tier(value: str) -> Literal["A", "B", "C"]:
    """Validate a release tier before using it in a typed capability."""
    if value not in _TIERS:
        raise ValueError("I/O capability tier must be A, B, or C")
    return value  # type: ignore[return-value]


def _effective_status(
    value: object,
) -> Literal["verified", "experimental", "unavailable"]:
    """Validate one worker result status with an exact literal return type."""
    candidate = _text(value, "status")
    if candidate == "verified":
        return "verified"
    if candidate == "experimental":
        return "experimental"
    if candidate == "unavailable":
        return "unavailable"
    raise ValueError("I/O capability snapshot status is invalid")


def _unique_fields(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Reject duplicate JSON keys rather than silently selecting one policy value."""
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("Manifest has duplicate JSON fields")
    return result


def _reject_constant(value: str) -> None:
    """Reject non-finite JSON constants from an artifact policy."""
    raise ValueError(f"Manifest has invalid JSON constant {value!r}")


def _text(value: object, label: str) -> str:
    """Return a bounded, printable manifest field without accepting arbitrary data."""
    if type(value) is not str or not value:
        raise ValueError(f"Manifest {label} must be a nonempty string")
    return value


def _format(value: object) -> str:
    """Validate one native format token without accepting a path-like value."""
    candidate = _text(value, "format")
    if _FORMAT.fullmatch(candidate) is None:
        raise ValueError("Manifest format is not a safe native format token")
    return candidate


def _caveat(value: object) -> str:
    """Validate bounded presentation text that cannot contain paths or newlines."""
    candidate = _text(value, "caveat")
    if _CAVEAT.fullmatch(candidate) is None:
        raise ValueError("Manifest caveat is not safe presentation text")
    return candidate


def _known_io_classes(io_classes: Collection[str] | None) -> frozenset[str]:
    """Return the static Studio class vocabulary without importing a backend."""
    if io_classes is None:
        io_classes = KNOWN_IO_CLASSES
    return frozenset(io_classes)


def _entry(value: object, *, io_classes: frozenset[str]) -> IOCapability:
    """Validate one exact, direction-level schema record."""
    if type(value) is not dict:
        raise ValueError("Manifest entry must be an object")
    tier_value = _tier(_text(value.get("tier"), "tier"))
    expected = {"datatype", "format", "direction", "tier"}
    if tier_value == "B":
        expected.add("caveat")
    if tier_value == "C":
        expected.add("reason")
    if set(value) != expected:
        raise ValueError("Manifest entry has unsupported fields")
    datatype = _text(value["datatype"], "datatype")
    if datatype not in io_classes:
        raise ValueError("Manifest datatype is unsupported")
    direction = _direction(_text(value["direction"], "direction"))
    format_name = _format(value["format"])
    caveat = _caveat(value["caveat"]) if tier_value == "B" else None
    reason = _text(value["reason"], "reason") if tier_value == "C" else None
    if reason is not None and reason not in TIER_C_REASONS:
        raise ValueError("Manifest Tier C reason is unsupported")
    return IOCapability(
        datatype=datatype,
        format=format_name,
        direction=direction,
        tier=tier_value,
        caveat=caveat,
        reason=reason,
    )


def _effective_entry(value: object) -> EffectiveIOCapability:
    """Decode one strict, path-free worker snapshot direction record."""
    if type(value) is not dict:
        raise ValueError("I/O capability snapshot entry must be an object")
    expected = {
        "datatype",
        "format",
        "direction",
        "tier",
        "status",
        "native_available",
        "auto_identify",
    }
    tier_value = _tier(_text(value.get("tier"), "tier"))
    status = _effective_status(value.get("status"))
    if tier_value == "A" and status not in {"verified", "unavailable"}:
        raise ValueError("I/O capability snapshot Tier A status is invalid")
    if tier_value == "B" and status not in {"experimental", "unavailable"}:
        raise ValueError("I/O capability snapshot Tier B status is invalid")
    if tier_value == "C" and status != "unavailable":
        raise ValueError("I/O capability snapshot Tier C status is invalid")
    if status == "unavailable":
        expected.add("reason")
    if tier_value == "B":
        expected.add("caveat")
    if set(value) != expected:
        raise ValueError("I/O capability snapshot entry has unsupported fields")
    native_available = value["native_available"]
    auto_identify = value["auto_identify"]
    if type(native_available) is not bool or type(auto_identify) is not bool:
        raise ValueError("I/O capability snapshot native facts are invalid")
    if status in {"verified", "experimental"} and not native_available:
        raise ValueError("I/O capability snapshot available entry lacks native route")
    if auto_identify and not native_available:
        raise ValueError("I/O capability snapshot cannot auto-identify a missing entry")
    caveat = _caveat(value["caveat"]) if "caveat" in value else None
    reason = _text(value["reason"], "reason") if "reason" in value else None
    if status == "unavailable":
        if reason not in UNAVAILABLE_REASONS:
            raise ValueError("I/O capability snapshot reason is invalid")
    elif reason is not None:
        raise ValueError("I/O capability snapshot available entry has a reason")
    datatype = _text(value["datatype"], "datatype")
    if datatype not in KNOWN_IO_CLASSES:
        raise ValueError("I/O capability snapshot datatype is unsupported")
    return EffectiveIOCapability(
        datatype=datatype,
        format=_format(value["format"]),
        direction=_direction(_text(value["direction"], "direction")),
        tier=tier_value,
        status=status,
        native_available=native_available,
        auto_identify=auto_identify,
        caveat=caveat,
        reason=reason,
    )


def _canonical_bytes(entries: tuple[IOCapability, ...]) -> bytes:
    """Serialize semantic policy content in its single digest representation."""
    document = {
        "schema_version": CAPABILITY_SCHEMA_VERSION,
        "entries": [
            entry.document()
            for entry in sorted(
                entries,
                key=lambda item: (item.datatype, item.format, item.direction),
            )
        ],
    }
    return json.dumps(
        document,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")


def _decode_manifest(
    content: bytes, *, io_classes: Collection[str] | None = None
) -> CapabilityManifest:
    """Decode one strict manifest and calculate its canonical semantic digest."""
    try:
        document = json.loads(
            content.decode("utf-8"),
            object_pairs_hook=_unique_fields,
            parse_constant=_reject_constant,
        )
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise ValueError("I/O capability manifest is malformed") from exc
    if type(document) is not dict or set(document) != {"schema_version", "entries"}:
        raise ValueError("I/O capability manifest schema is invalid")
    if type(document["schema_version"]) is not int or document["schema_version"] != 1:
        raise ValueError("I/O capability manifest version is unsupported")
    raw_entries = document["entries"]
    if type(raw_entries) is not list or len(raw_entries) > MAX_MANIFEST_ENTRIES:
        raise ValueError("I/O capability manifest entries are invalid")
    classes = _known_io_classes(io_classes)
    entries = tuple(_entry(value, io_classes=classes) for value in raw_entries)
    keys = {(entry.datatype, entry.format, entry.direction) for entry in entries}
    if len(keys) != len(entries):
        raise ValueError("I/O capability manifest has duplicate directions")
    digest = hashlib.sha256(_canonical_bytes(entries)).hexdigest()
    return CapabilityManifest(mode="frozen", entries=entries, digest=digest)


def _read_manifest(path_value: str) -> bytes:
    """Read one bounded regular manifest without following an artifact symlink."""
    if not path_value or not Path(path_value).is_absolute():
        raise ValueError("Manifest path is invalid")
    flags = (
        os.O_RDONLY
        | os.O_NONBLOCK
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.open(path_value, flags)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_MANIFEST_BYTES:
            raise ValueError("Manifest is not a bounded regular file")
        chunks: list[bytes] = []
        remaining = MAX_MANIFEST_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        content = b"".join(chunks)
        if len(content) > MAX_MANIFEST_BYTES:
            raise ValueError("Manifest exceeds size limit")
        return content
    finally:
        os.close(descriptor)


def load_capability_manifest(
    *,
    environ: Mapping[str, str] | None = None,
    io_classes: Collection[str] | None = None,
) -> CapabilityManifest:
    """Load the optional frozen-artifact contract, failing closed on every error.

    The absent environment variable is the intentional source-development mode.
    An explicitly configured file that cannot be safely decoded is *not* treated
    as development mode, because that would let a damaged artifact bypass its
    release capability policy.
    """
    values = os.environ if environ is None else environ
    path_value = values.get(CAPABILITY_ENVIRONMENT)
    if path_value is None:
        return CapabilityManifest(mode="developer")
    try:
        return _decode_manifest(_read_manifest(path_value), io_classes=io_classes)
    except (OSError, UnicodeError, ValueError, RecursionError):
        return CapabilityManifest(mode="invalid")


def current_capability_digest() -> str | None:
    """Return only the safe canonical digest for About or diagnostics callers."""
    return load_capability_manifest().digest


_ACTIVE_EFFECTIVE_SNAPSHOT: EffectiveCapabilitySnapshot | None = None


def activate_effective_capability_snapshot(
    snapshot: EffectiveCapabilitySnapshot | None,
) -> None:
    """Install one worker-local runtime result for I/O enforcement.

    The Studio worker is a dedicated process.  This intentionally process-local
    state keeps GUI processes from probing or importing an optional I/O backend.
    Passing ``None`` is used only by unit-test cleanup and worker teardown.
    """
    if snapshot is not None and not isinstance(snapshot, EffectiveCapabilitySnapshot):
        raise TypeError("Effective I/O capability snapshot has an invalid type")
    global _ACTIVE_EFFECTIVE_SNAPSHOT
    _ACTIVE_EFFECTIVE_SNAPSHOT = snapshot


def current_effective_capability_snapshot() -> EffectiveCapabilitySnapshot | None:
    """Return the current worker-local snapshot without triggering a probe."""
    return _ACTIVE_EFFECTIVE_SNAPSHOT


def _snapshot(
    *,
    mode: Literal["developer", "frozen", "invalid"],
    policy_digest: str | None,
    entries: tuple[EffectiveIOCapability, ...] = (),
) -> EffectiveCapabilitySnapshot:
    """Create a canonical immutable snapshot from trusted worker probe facts."""
    if len(entries) > MAX_EFFECTIVE_CAPABILITY_ENTRIES:
        raise ValueError("I/O capability snapshot exceeds control-message limit")
    ordered = tuple(
        sorted(entries, key=lambda item: (item.datatype, item.format, item.direction))
    )
    digest = hashlib.sha256(
        _effective_canonical_bytes(
            mode=mode,
            policy_digest=policy_digest,
            entries=ordered,
        )
    ).hexdigest()
    return EffectiveCapabilitySnapshot(
        mode=mode,
        entries=ordered,
        policy_digest=policy_digest,
        digest=digest,
    )


def _truthy_registry_value(value: object) -> bool:
    """Interpret registry booleans without accepting arbitrary truthiness."""
    return (
        type(value) is bool
        and value
        or (isinstance(value, str) and value.lower() in {"yes", "true", "1"})
    )


def _runtime_failure_reason(error: Exception) -> str:
    """Map worker-only backend failures to a fixed public reason enum."""
    if isinstance(error, (ImportError, ModuleNotFoundError)):
        return "runtime_dependency_missing"
    return "probe_failed"


def _probe_registered_route(
    registries: tuple[object, ...],
    *,
    format_name: str,
    datatype: object,
    direction: Literal["read", "write"],
) -> str | None:
    """Probe a registered reader/writer without opening user data.

    Some registry implementations expose no public ``get_reader``/``get_writer``
    lookup.  Registration itself remains their data-free probe in that case.
    Where lookup exists, one successful registry is enough: a format may be
    registered through either the GWpy or Astropy registry.
    """
    method_name = f"get_{direction}er"
    failures: list[str] = []
    looked_up = False
    for registry in registries:
        getter = getattr(registry, method_name, None)
        if not callable(getter):
            return None
        looked_up = True
        try:
            getter(format_name, datatype)
        except Exception as error:
            failures.append(_runtime_failure_reason(error))
        else:
            return None
    if not looked_up or not failures:
        return None
    return (
        "runtime_dependency_missing"
        if "runtime_dependency_missing" in failures
        else "probe_failed"
    )


def _native_registry_facts(
    io_module: Any,
    *,
    datatype_name: str,
    limit: int,
    reviewed_keys: frozenset[tuple[str, Literal["read", "write"]]],
) -> tuple[
    dict[tuple[str, Literal["read", "write"]], tuple[bool, bool, tuple[object, ...]]],
    dict[Literal["read", "write"], str],
]:
    """Return data-free native registry facts and per-direction probe failures."""
    native: dict[
        tuple[str, Literal["read", "write"]], tuple[bool, bool, tuple[object, ...]]
    ] = {}
    unreviewed_count = 0
    failures: dict[Literal["read", "write"], str] = {}
    try:
        datatype = io_module.io_class(datatype_name)
    except Exception as error:
        reason = _runtime_failure_reason(error)
        return native, {"read": reason, "write": reason}
    try:
        registries = tuple(io_module._io_registries())
    except Exception as error:
        reason = _runtime_failure_reason(error)
        return native, {"read": reason, "write": reason}

    for direction in _IO_DIRECTIONS:
        successful_registry_query = False
        query_failures: list[str] = []
        for registry in registries:
            try:
                table = registry.get_formats(datatype, direction.title())
            except Exception as error:
                query_failures.append(_runtime_failure_reason(error))
                continue
            successful_registry_query = True
            columns = getattr(table, "colnames", ())
            if not isinstance(columns, Collection):
                continue
            for row in table:
                try:
                    name = _format(str(row["Format"]))
                    available = _truthy_registry_value(row[direction.title()])
                    auto_identify = (
                        direction == "read"
                        and "Auto-identify" in columns
                        and _truthy_registry_value(row["Auto-identify"])
                    )
                except (KeyError, TypeError, ValueError):
                    continue
                if not available:
                    continue
                key = (name, direction)
                prior = native.get(key)
                if prior is None:
                    if key not in reviewed_keys and unreviewed_count == limit:
                        raise _CapabilitySnapshotLimitError
                    native[key] = (True, auto_identify, (registry,))
                    if key not in reviewed_keys:
                        unreviewed_count += 1
                else:
                    native[key] = (
                        True,
                        prior[1] or auto_identify,
                        (*prior[2], registry),
                    )
        if not successful_registry_query:
            failures[direction] = (
                "runtime_dependency_missing"
                if "runtime_dependency_missing" in query_failures
                else "probe_failed"
            )
    return native, failures


def probe_effective_capabilities(
    manifest: CapabilityManifest,
    *,
    io_module: Any | None = None,
) -> EffectiveCapabilitySnapshot:
    """Build a frozen-artifact I/O snapshot in the worker after startup.

    Callers must run this only in the worker process.  It registers and probes
    public I/O routes without opening user data, then intersects the result
    with the reviewed artifact policy.  GUI code receives only the returned
    safe document, never backend exceptions or import attempts.
    """
    if manifest.mode != "frozen":
        return _snapshot(mode=manifest.mode, policy_digest=None)
    if io_module is None:
        from . import io as native_io_module

        io_module = native_io_module

    assert io_module is not None

    policy = {
        (entry.datatype, entry.format, entry.direction): entry
        for entry in manifest.entries
    }
    if len(policy) > MAX_EFFECTIVE_CAPABILITY_ENTRIES:
        return _snapshot(mode="invalid", policy_digest=None)
    native: dict[
        tuple[str, str, Literal["read", "write"]],
        tuple[bool, bool, tuple[object, ...]],
    ] = {}
    failures: dict[tuple[str, Literal["read", "write"]], str] = {}
    classes = getattr(io_module, "IO_CLASSES", ())
    if not isinstance(classes, Collection):
        classes = ()
    for datatype_name in classes:
        if type(datatype_name) is not str:
            continue
        try:
            policy_keys = {
                (format_name, direction)
                for datatype, format_name, direction in policy
                if datatype == datatype_name
            }
            unreviewed_native_count = len(set(native) - set(policy))
            found, datatype_failures = _native_registry_facts(
                io_module,
                datatype_name=datatype_name,
                limit=(
                    MAX_EFFECTIVE_CAPABILITY_ENTRIES
                    - len(policy)
                    - unreviewed_native_count
                ),
                reviewed_keys=frozenset(policy_keys),
            )
        except _CapabilitySnapshotLimitError:
            return unprobed_effective_capabilities(manifest)
        for (format_name, direction), value in found.items():
            native[(datatype_name, format_name, direction)] = value
        for direction, failure_reason in datatype_failures.items():
            failures[(datatype_name, direction)] = failure_reason

    effective: list[EffectiveIOCapability] = []
    all_keys = set(policy) | set(native)
    for datatype_name, format_name, direction in sorted(all_keys):
        entry = policy.get((datatype_name, format_name, direction))
        native_fact = native.get((datatype_name, format_name, direction))
        native_available = native_fact is not None and native_fact[0]
        auto_identify = bool(native_fact[1]) if native_fact is not None else False
        if entry is None:
            effective.append(
                EffectiveIOCapability(
                    datatype=datatype_name,
                    format=format_name,
                    direction=direction,
                    tier="C",
                    status="unavailable",
                    native_available=native_available,
                    auto_identify=auto_identify,
                    reason="unreviewed_registry_entry",
                )
            )
            continue
        if entry.tier == "C":
            effective.append(
                EffectiveIOCapability(
                    datatype=entry.datatype,
                    format=entry.format,
                    direction=entry.direction,
                    tier="C",
                    status="unavailable",
                    native_available=native_available,
                    auto_identify=auto_identify,
                    reason=entry.reason or "frozen_unverified",
                )
            )
            continue
        reason: str | None = None
        if not native_available:
            reason = failures.get((datatype_name, direction), "registry_missing")
        elif native_fact is not None:
            reason = _probe_registered_route(
                native_fact[2],
                format_name=format_name,
                datatype=io_module.io_class(datatype_name),
                direction=direction,
            )
        if reason is not None:
            effective.append(
                EffectiveIOCapability(
                    datatype=entry.datatype,
                    format=entry.format,
                    direction=entry.direction,
                    tier=entry.tier,
                    status="unavailable",
                    native_available=native_available,
                    auto_identify=auto_identify,
                    caveat=entry.caveat,
                    reason=reason,
                )
            )
        else:
            effective.append(
                EffectiveIOCapability(
                    datatype=entry.datatype,
                    format=entry.format,
                    direction=entry.direction,
                    tier=entry.tier,
                    status="verified" if entry.tier == "A" else "experimental",
                    native_available=True,
                    auto_identify=auto_identify,
                    caveat=entry.caveat,
                )
            )
    return _snapshot(
        mode="frozen",
        policy_digest=manifest.digest,
        entries=tuple(effective),
    )


def unprobed_effective_capabilities(
    manifest: CapabilityManifest,
) -> EffectiveCapabilitySnapshot:
    """Fail closed when frozen I/O is reached without worker bootstrap facts.

    This is deliberately separate from :func:`probe_effective_capabilities`:
    it does not import a backend or attempt a registry lookup.  It protects an
    accidental direct I/O call before worker startup while preserving ordinary
    source-development behavior when no artifact manifest is configured.
    """
    if manifest.mode != "frozen":
        return _snapshot(mode=manifest.mode, policy_digest=None)
    if len(manifest.entries) > MAX_EFFECTIVE_CAPABILITY_ENTRIES:
        return _snapshot(mode="invalid", policy_digest=None)
    entries = tuple(
        EffectiveIOCapability(
            datatype=entry.datatype,
            format=entry.format,
            direction=entry.direction,
            tier=entry.tier,
            status="unavailable",
            native_available=False,
            auto_identify=False,
            caveat=entry.caveat,
            reason=entry.reason if entry.tier == "C" else "probe_failed",
        )
        for entry in manifest.entries
    )
    return _snapshot(
        mode="frozen",
        policy_digest=manifest.digest,
        entries=entries,
    )


def capability_annotation(
    manifest: CapabilityManifest | EffectiveCapabilitySnapshot,
    *,
    datatype: str,
    format_name: str,
    direction: str,
    native_available: bool,
) -> dict[str, bool | str | None]:
    """Describe a catalog direction without changing native registry facts."""
    if isinstance(manifest, EffectiveCapabilitySnapshot):
        entry = manifest.capability(datatype, format_name, direction)
        if entry is None:
            return {
                "available": native_available,
                "tier": None,
                "caveat": None,
                "reason": None,
            }
        return {
            "available": entry.available,
            "tier": entry.tier,
            "caveat": entry.caveat,
            "reason": entry.reason,
            "status": entry.status,
        }
    if not manifest.active:
        return {
            "available": native_available,
            "tier": None,
            "caveat": None,
            "reason": None,
        }
    static_entry = manifest.capability(datatype, format_name, direction)
    assert static_entry is not None
    if not native_available:
        return {
            "available": False,
            "tier": "C",
            "caveat": None,
            "reason": (
                static_entry.reason if static_entry.tier == "C" else "frozen_unverified"
            ),
        }
    return {
        "available": static_entry.tier in {"A", "B"},
        "tier": static_entry.tier,
        "caveat": static_entry.caveat,
        "reason": static_entry.reason,
    }
