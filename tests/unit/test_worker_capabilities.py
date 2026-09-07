"""Worker-only effective I/O capability contracts."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.unit


class _Table(list[dict[str, str]]):
    """Small registry-table double with Astropy's public ``colnames`` shape."""

    colnames = ("Format", "Read", "Write", "Auto-identify")


class _Registry:
    """Data-free native registry double used by effective-probe tests."""

    def __init__(
        self,
        rows: list[dict[str, str]],
        *,
        reader_failures: dict[str, BaseException] | None = None,
    ) -> None:
        self._rows = rows
        self._reader_failures = reader_failures or {}

    def get_formats(self, _datatype: object, _direction: str) -> _Table:
        return _Table(self._rows)

    def get_reader(self, format_name: str, _datatype: object) -> object:
        failure = self._reader_failures.get(format_name)
        if failure is not None:
            raise failure
        return object()


class _RegistryText(str):
    """Mirror registry scalar string subclasses such as ``numpy.str_``."""


def _manifest(*entries: object):
    from gwexpy_studio.ops.io_capabilities import CapabilityManifest

    return CapabilityManifest(
        mode="frozen",
        entries=tuple(entries),
        digest="a" * 64,
    )


def _entry(format_name: str, tier: str, **extra: str):
    from gwexpy_studio.ops.io_capabilities import IOCapability

    return IOCapability(
        datatype="TimeSeries",
        format=format_name,
        direction="read",
        tier=tier,  # type: ignore[arg-type]
        **extra,
    )


def _io_module(registry: _Registry) -> SimpleNamespace:
    return SimpleNamespace(
        IO_CLASSES=("TimeSeries",),
        io_class=lambda _datatype: object,
        _io_registries=lambda: (registry,),
    )


@pytest.mark.contract("REL-IOC-016")
def test_worker_probe_intersects_policy_with_registry_and_preserves_missing_entries():
    """A/B availability is worker-probed; unseen entries fail closed."""
    from gwexpy_studio.ops.io_capabilities import probe_effective_capabilities

    registry = _Registry(
        [
            {
                "Format": "ready",
                "Read": "Yes",
                "Write": "No",
                "Auto-identify": "Yes",
            },
            {
                "Format": "limited",
                "Read": "Yes",
                "Write": "No",
                "Auto-identify": "No",
            },
            {
                "Format": "unreviewed",
                "Read": "Yes",
                "Write": "No",
                "Auto-identify": "No",
            },
        ]
    )
    snapshot = probe_effective_capabilities(
        _manifest(
            _entry("ready", "A"),
            _entry("limited", "B", caveat="Name is not saved"),
            _entry("gone", "A"),
        ),
        io_module=_io_module(registry),
    )

    assert snapshot.policy_digest == "a" * 64
    assert snapshot.capability("TimeSeries", "ready", "read").document() == {
        "datatype": "TimeSeries",
        "format": "ready",
        "direction": "read",
        "tier": "A",
        "status": "verified",
        "native_available": True,
        "auto_identify": True,
    }
    assert snapshot.capability("TimeSeries", "limited", "read").document() == {
        "datatype": "TimeSeries",
        "format": "limited",
        "direction": "read",
        "tier": "B",
        "status": "experimental",
        "native_available": True,
        "auto_identify": False,
        "caveat": "Name is not saved",
    }
    assert (
        snapshot.capability("TimeSeries", "gone", "read").reason == "registry_missing"
    )
    assert (
        snapshot.capability("TimeSeries", "unreviewed", "read").reason
        == "unreviewed_registry_entry"
    )


@pytest.mark.contract("REL-IOC-017")
def test_worker_probe_maps_import_and_probe_failures_to_distinct_safe_reasons():
    """Worker-only probe retains a safe cause class without backend details."""
    from gwexpy_studio.ops.io_capabilities import probe_effective_capabilities

    registry = _Registry(
        [
            {
                "Format": "missing",
                "Read": "Yes",
                "Write": "No",
                "Auto-identify": "No",
            },
            {
                "Format": "broken",
                "Read": "Yes",
                "Write": "No",
                "Auto-identify": "No",
            },
        ],
        reader_failures={
            "missing": ModuleNotFoundError("private optional backend"),
            "broken": RuntimeError("/private/backend/trace"),
        },
    )
    snapshot = probe_effective_capabilities(
        _manifest(_entry("missing", "A"), _entry("broken", "A")),
        io_module=_io_module(registry),
    )

    assert snapshot.capability("TimeSeries", "missing", "read").reason == (
        "runtime_dependency_missing"
    )
    assert snapshot.capability("TimeSeries", "broken", "read").reason == (
        "probe_failed"
    )


@pytest.mark.contract("REL-IOC-021")
def test_worker_probe_accepts_registry_string_scalar_subclasses() -> None:
    """Astropy table scalars remain a closed Yes/No vocabulary, not truthiness."""
    from gwexpy_studio.ops.io_capabilities import probe_effective_capabilities

    snapshot = probe_effective_capabilities(
        _manifest(_entry("csv", "A")),
        io_module=_io_module(
            _Registry(
                [
                    {
                        "Format": "csv",
                        "Read": _RegistryText("Yes"),
                        "Write": _RegistryText("No"),
                        "Auto-identify": _RegistryText("Yes"),
                    }
                ]
            )
        ),
    )

    entry = snapshot.capability("TimeSeries", "csv", "read")
    assert entry is not None
    assert entry.status == "verified"
    assert entry.auto_identify is True


def test_effective_capability_limit_keeps_a_maximum_policy_ping_under_control_limit(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """The largest accepted policy still fits in one real protocol envelope."""
    from gwexpy_studio.ops.io_capabilities import (
        MAX_EFFECTIVE_CAPABILITY_ENTRIES,
        load_capability_manifest,
        unprobed_effective_capabilities,
    )
    from gwexpy_studio.worker.protocol import (
        CONTROL_MESSAGE_LIMIT_BYTES,
        encode_message,
    )

    caveat = "C" + "a" * 159
    entries = [
        {
            "datatype": "TimeSeries",
            "format": f"f{index:0126x}",
            "direction": "read",
            "tier": "B",
            "caveat": caveat,
        }
        for index in range(MAX_EFFECTIVE_CAPABILITY_ENTRIES)
    ]
    manifest_path = tmp_path / "io-capabilities.json"
    manifest_path.write_text(
        json.dumps({"schema_version": 1, "entries": entries}), encoding="utf-8"
    )
    monkeypatch.setenv("GWEXPY_STUDIO_IO_CAPABILITIES", str(manifest_path))
    snapshot = unprobed_effective_capabilities(load_capability_manifest())

    encoded = encode_message(
        {
            "protocol": 2,
            "request_id": "123e4567-e89b-42d3-a456-426614174000",
            "type": "result",
            "payload": {
                "ready": True,
                "request_id": "123e4567-e89b-42d3-a456-426614174000",
                "pid": 1,
                "python": "3.12.12",
                "gwexpy_version": "0.2.0",
                "gwexpy_path": "",
                "io_capabilities": snapshot.document(),
            },
        }
    )

    assert len(snapshot.entries) == MAX_EFFECTIVE_CAPABILITY_ENTRIES
    assert len(encoded) <= CONTROL_MESSAGE_LIMIT_BYTES


def test_unreviewed_registry_overflow_returns_entire_unprobed_snapshot() -> None:
    """Unreviewed native formats cannot produce a partial or oversized ping."""
    from gwexpy_studio.ops.io_capabilities import (
        MAX_EFFECTIVE_CAPABILITY_ENTRIES,
        probe_effective_capabilities,
    )

    registry = _Registry(
        [
            {
                "Format": f"f{index:0126x}",
                "Read": "Yes",
                "Write": "No",
                "Auto-identify": "No",
            }
            for index in range(MAX_EFFECTIVE_CAPABILITY_ENTRIES + 1)
        ]
    )
    snapshot = probe_effective_capabilities(
        _manifest(_entry("reviewed", "A")), io_module=_io_module(registry)
    )

    assert len(snapshot.entries) == 1
    assert snapshot.capability("TimeSeries", "reviewed", "read").status == "unavailable"
    assert (
        snapshot.capability("TimeSeries", "reviewed", "read").reason == "probe_failed"
    )


def test_maximum_policy_matching_native_routes_do_not_consume_unreviewed_budget() -> (
    None
):
    """Reviewed routes remain probeable when the effective snapshot is full."""
    from gwexpy_studio.ops.io_capabilities import (
        MAX_EFFECTIVE_CAPABILITY_ENTRIES,
        probe_effective_capabilities,
    )

    formats = tuple(
        f"f{index:0126x}" for index in range(MAX_EFFECTIVE_CAPABILITY_ENTRIES)
    )
    registry = _Registry(
        [
            {
                "Format": format_name,
                "Read": "Yes",
                "Write": "No",
                "Auto-identify": "No",
            }
            for format_name in formats
        ]
    )
    snapshot = probe_effective_capabilities(
        _manifest(*(_entry(format_name, "A") for format_name in formats)),
        io_module=_io_module(registry),
    )

    assert len(snapshot.entries) == MAX_EFFECTIVE_CAPABILITY_ENTRIES
    assert snapshot.capability("TimeSeries", formats[-1], "read").status == "verified"


def test_snapshot_validator_rejects_policy_omission_tier_escalation_and_added_a() -> (
    None
):
    """A schema-valid worker document cannot widen or weaken frozen policy."""
    from dataclasses import replace

    from gwexpy_studio.ops.io_capabilities import (
        EffectiveIOCapability,
        _snapshot,
        validate_effective_capability_snapshot,
    )

    manifest = _manifest(
        _entry("approved", "B", caveat="Name is not saved"),
        _entry("blocked", "C", reason="native_error"),
    )
    approved = EffectiveIOCapability(
        datatype="TimeSeries",
        format="approved",
        direction="read",
        tier="B",
        status="experimental",
        native_available=True,
        auto_identify=False,
        caveat="Name is not saved",
    )
    blocked = EffectiveIOCapability(
        datatype="TimeSeries",
        format="blocked",
        direction="read",
        tier="C",
        status="unavailable",
        native_available=False,
        auto_identify=False,
        reason="native_error",
    )
    valid = _snapshot(
        mode="frozen", policy_digest="a" * 64, entries=(approved, blocked)
    )
    validate_effective_capability_snapshot(valid, manifest)

    for entries in (
        (blocked,),
        (replace(approved, tier="A", status="verified"), blocked),
        (
            approved,
            blocked,
            EffectiveIOCapability(
                datatype="TimeSeries",
                format="injected",
                direction="read",
                tier="A",
                status="verified",
                native_available=True,
                auto_identify=False,
            ),
        ),
    ):
        candidate = _snapshot(mode="frozen", policy_digest="a" * 64, entries=entries)
        with pytest.raises(ValueError, match="policy"):
            validate_effective_capability_snapshot(candidate, manifest)


def test_snapshot_rejects_unavailable_tier_b_entry_without_its_caveat() -> None:
    """Tier B caveats remain mandatory when a worker marks the route unavailable."""
    from gwexpy_studio.ops.io_capabilities import EffectiveCapabilitySnapshot

    document = {
        "schema_version": 1,
        "mode": "frozen",
        "policy_digest": "a" * 64,
        "entries": [
            {
                "datatype": "TimeSeries",
                "format": "limited",
                "direction": "read",
                "tier": "B",
                "status": "unavailable",
                "native_available": False,
                "auto_identify": False,
                "reason": "registry_missing",
            }
        ],
        "digest": "b" * 64,
    }

    with pytest.raises(ValueError, match="unsupported fields"):
        EffectiveCapabilitySnapshot.from_document(document)


def test_snapshot_rejects_boolean_schema_version() -> None:
    """A bool is never accepted as the integer wire-schema discriminator."""
    from gwexpy_studio.ops.io_capabilities import EffectiveCapabilitySnapshot

    with pytest.raises(ValueError, match="version is unsupported"):
        EffectiveCapabilitySnapshot.from_document(
            {
                "schema_version": True,
                "mode": "developer",
                "policy_digest": None,
                "entries": [],
                "digest": "a" * 64,
            }
        )


@pytest.mark.contract("REL-IOC-020")
def test_gui_import_does_not_load_optional_io_backends() -> None:
    """The GUI validates worker facts without importing GWpy/GWexpy backends."""
    environment = dict(os.environ)
    environment["QT_QPA_PLATFORM"] = "offscreen"
    result = subprocess.run(
        (
            sys.executable,
            "-c",
            "import json, sys; import gwexpy_studio.ui.window; "
            "print(json.dumps(sorted(name for name in sys.modules "
            "if name == 'gwexpy' or name.startswith('gwexpy.') "
            "or name == 'gwpy' or name.startswith('gwpy.'))))",
        ),
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.splitlines()[-1]) == []
