"""Frozen-artifact I/O capability contracts without native backend fixtures."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture(autouse=True)
def _clear_worker_snapshot() -> object:
    """Keep process-local worker qualification isolated across static-policy tests."""
    from gwexpy_studio.ops.io_capabilities import activate_effective_capability_snapshot

    activate_effective_capability_snapshot(None)
    yield
    activate_effective_capability_snapshot(None)


def _entry(
    datatype: str,
    format_name: str,
    direction: str,
    tier: str,
    *,
    caveat: str | None = None,
    reason: str | None = None,
) -> dict[str, str]:
    entry = {
        "datatype": datatype,
        "format": format_name,
        "direction": direction,
        "tier": tier,
    }
    if caveat is not None:
        entry["caveat"] = caveat
    if reason is not None:
        entry["reason"] = reason
    return entry


def _activate_manifest(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    entries: list[dict[str, str]],
) -> Path:
    path = tmp_path / "io-capabilities.json"
    path.write_text(
        json.dumps({"schema_version": 1, "entries": entries}), encoding="utf-8"
    )
    monkeypatch.setenv("GWEXPY_STUDIO_IO_CAPABILITIES", str(path))
    return path


def _qualify_active_manifest(*, extra_read_formats: tuple[str, ...] = ()) -> None:
    """Install a data-free worker snapshot matching the current test manifest."""
    from gwexpy_studio.ops.io_capabilities import (
        activate_effective_capability_snapshot,
        load_capability_manifest,
        probe_effective_capabilities,
    )

    manifest = load_capability_manifest()
    rows: dict[str, dict[str, str]] = {}
    for entry in manifest.entries:
        row = rows.setdefault(
            entry.format,
            {
                "Format": entry.format,
                "Read": "No",
                "Write": "No",
                "Auto-identify": "No",
            },
        )
        row[entry.direction.title()] = "Yes"
        if entry.direction == "read":
            row["Auto-identify"] = "Yes"
    for format_name in extra_read_formats:
        rows[format_name] = {
            "Format": format_name,
            "Read": "Yes",
            "Write": "No",
            "Auto-identify": "No",
        }

    class Table(list[dict[str, str]]):
        colnames = ("Format", "Read", "Write", "Auto-identify")

    registry = SimpleNamespace(
        get_formats=lambda *_args: Table(rows.values()),
        get_reader=lambda *_args: object(),
        get_writer=lambda *_args: object(),
    )
    io_module = SimpleNamespace(
        IO_CLASSES=("TimeSeries",),
        io_class=lambda _datatype: object,
        _io_registries=lambda: (registry,),
    )
    activate_effective_capability_snapshot(
        probe_effective_capabilities(manifest, io_module=io_module)
    )


@pytest.mark.contract("REL-IOC-001")
def test_absent_manifest_keeps_developer_mode_unrestricted(monkeypatch):
    """An unset artifact contract must not constrain source development I/O."""
    from gwexpy_studio.ops.io_capabilities import load_capability_manifest

    monkeypatch.delenv("GWEXPY_STUDIO_IO_CAPABILITIES", raising=False)
    manifest = load_capability_manifest()

    assert manifest.mode == "developer"
    assert manifest.digest is None
    assert manifest.capability("TimeSeries", "future_native", "read") is None


@pytest.mark.contract("REL-IOC-002")
def test_valid_manifest_has_a_stable_canonical_digest(monkeypatch, tmp_path):
    """Semantic entry order does not change the published capability digest."""
    from gwexpy_studio.ops.io_capabilities import load_capability_manifest

    entries = [
        _entry("TimeSeries", "zformat", "write", "C", reason="native_error"),
        _entry("TimeSeries", "aformat", "read", "B", caveat="Name is not saved"),
        _entry("TimeSeries", "aformat", "write", "A"),
    ]
    _activate_manifest(monkeypatch, tmp_path, entries)
    first = load_capability_manifest()
    _activate_manifest(monkeypatch, tmp_path, list(reversed(entries)))
    second = load_capability_manifest()

    assert first.mode == "frozen"
    assert len(first.digest or "") == 64
    assert first.digest == second.digest
    assert first.capability("TimeSeries", "aformat", "read").tier == "B"


@pytest.mark.contract("REL-IOC-003")
def test_invalid_manifest_fails_closed_without_disclosing_its_path(
    monkeypatch, tmp_path
):
    """An unreadable or malformed artifact contract disables every direction safely."""
    from gwexpy_studio.ops.io_capabilities import (
        IOCapabilityError,
        load_capability_manifest,
    )

    path = tmp_path / "private experiment capability manifest.json"
    path.write_text("{not json", encoding="utf-8")
    monkeypatch.setenv("GWEXPY_STUDIO_IO_CAPABILITIES", str(path))
    manifest = load_capability_manifest()

    assert manifest.mode == "invalid"
    assert manifest.digest is None
    with pytest.raises(IOCapabilityError) as raised:
        manifest.require("TimeSeries", "hdf5", "read")
    assert raised.value.code == "io_capability_unavailable"
    assert raised.value.reason == "frozen_unverified"
    assert str(path) not in str(raised.value)


@pytest.mark.contract("REL-IOC-004")
def test_invalid_schema_and_duplicate_direction_entries_fail_closed(
    monkeypatch, tmp_path
):
    """Strict schema rejects policy ambiguity instead of accepting partial policy."""
    from gwexpy_studio.ops.io_capabilities import load_capability_manifest

    _activate_manifest(
        monkeypatch,
        tmp_path,
        [
            _entry("TimeSeries", "hdf5", "read", "A"),
            _entry("TimeSeries", "hdf5", "read", "C", reason="native_error"),
        ],
    )

    assert load_capability_manifest().mode == "invalid"


def test_manifest_above_effective_snapshot_cap_fails_closed(monkeypatch, tmp_path):
    """A policy cannot be accepted if its worker snapshot would exceed ping bounds."""
    from gwexpy_studio.ops.io_capabilities import (
        MAX_EFFECTIVE_CAPABILITY_ENTRIES,
        load_capability_manifest,
    )

    _activate_manifest(
        monkeypatch,
        tmp_path,
        [
            _entry("TimeSeries", f"f{index:0126x}", "read", "A")
            for index in range(MAX_EFFECTIVE_CAPABILITY_ENTRIES + 1)
        ],
    )

    assert load_capability_manifest().mode == "invalid"


@pytest.mark.contract("REL-IOC-005")
def test_active_tier_a_auto_read_resolves_then_reaches_native_api(
    monkeypatch, tmp_path
):
    """A declared Tier A auto-read resolves before the unchanged native class API."""
    from gwexpy_studio.ops import io

    _activate_manifest(
        monkeypatch,
        tmp_path,
        [_entry("TimeSeries", "identified", "read", "A")],
    )
    calls: list[tuple[object, tuple[object, ...], dict[str, object]]] = []

    class Native:
        @classmethod
        def read(cls, source, *args, **kwargs):
            calls.append((source, args, kwargs))
            return "native-result"

    monkeypatch.setattr(io, "io_class", lambda _datatype: Native)
    registry = SimpleNamespace(identify_format=lambda *_args: ["identified"])
    monkeypatch.setattr(io, "_io_registries", lambda: (registry,))
    _qualify_active_manifest()

    assert io.read_data("TimeSeries", "data.verified") == "native-result"
    assert calls == [("data.verified", (), {"format": "identified"})]


@pytest.mark.contract("REL-IOC-006")
def test_active_tier_c_or_missing_direction_blocks_before_native_read(
    monkeypatch, tmp_path
):
    """Unavailable and undeclared formats never reach a frozen artifact reader."""
    from gwexpy_studio.ops import io
    from gwexpy_studio.ops.io_capabilities import IOCapabilityError

    _activate_manifest(
        monkeypatch,
        tmp_path,
        [_entry("TimeSeries", "blocked", "read", "C", reason="backend_not_bundled")],
    )
    calls: list[object] = []

    class Native:
        @classmethod
        def read(cls, source, *args, **kwargs):
            calls.append(source)
            return "unexpected"

    monkeypatch.setattr(io, "io_class", lambda _datatype: Native)
    for format_name, reason in (
        ("blocked", "backend_not_bundled"),
        ("not-listed", "frozen_unverified"),
    ):
        with pytest.raises(IOCapabilityError) as raised:
            io.read_data("TimeSeries", "data.any", format=format_name)
        assert raised.value.code == "io_capability_unavailable"
        assert raised.value.reason == reason
    assert calls == []


@pytest.mark.contract("REL-IOC-007")
def test_auto_identification_enforces_the_resolved_frozen_direction(
    monkeypatch, tmp_path
):
    """Native auto-identification cannot bypass a Tier C artifact restriction."""
    from gwexpy_studio.ops import io
    from gwexpy_studio.ops.io_capabilities import IOCapabilityError

    _activate_manifest(
        monkeypatch,
        tmp_path,
        [_entry("TimeSeries", "identified", "read", "C", reason="native_error")],
    )

    class Native:
        @classmethod
        def read(cls, source, *args, **kwargs):
            raise AssertionError("capability check must precede native read")

    registry = SimpleNamespace(identify_format=lambda *_args: ["identified"])
    monkeypatch.setattr(io, "io_class", lambda _datatype: Native)
    monkeypatch.setattr(io, "_io_registries", lambda: (registry,))

    with pytest.raises(IOCapabilityError, match="unavailable") as raised:
        io.identify_io("TimeSeries", "data.auto")
    assert raised.value.reason == "native_error"
    with pytest.raises(IOCapabilityError):
        io.read_data("TimeSeries", "data.auto")


@pytest.mark.contract("REL-IOC-008")
def test_catalog_annotates_tier_b_and_missing_directions_with_digest(
    monkeypatch, tmp_path
):
    """The catalog distinguishes verified caveats from unverified native candidates."""
    from gwexpy_studio.ops import io

    _activate_manifest(
        monkeypatch,
        tmp_path,
        [
            _entry(
                "TimeSeries",
                "limited",
                "read",
                "B",
                caveat="Name is not saved",
            )
        ],
    )

    class Table(list[dict[str, str]]):
        colnames = ("Format", "Read", "Write", "Auto-identify")

    registry = SimpleNamespace(
        get_formats=lambda *_args: Table(
            [
                {
                    "Format": "limited",
                    "Read": "Yes",
                    "Write": "No",
                    "Auto-identify": "Yes",
                },
                {
                    "Format": "native-unverified",
                    "Read": "Yes",
                    "Write": "No",
                    "Auto-identify": "No",
                },
            ]
        )
    )
    monkeypatch.setattr(io, "io_class", lambda _datatype: object)
    monkeypatch.setattr(io, "_io_registries", lambda: (registry,))
    _qualify_active_manifest(extra_read_formats=("native-unverified",))

    catalog = io.io_catalog("TimeSeries", "read")
    entries = {entry["format"]: entry for entry in catalog["formats"]}
    limited = entries["limited"]["capabilities"]["read"]
    unverified = entries["native-unverified"]["capabilities"]["read"]

    assert catalog["capability_mode"] == "frozen"
    assert len(catalog["capability_digest"] or "") == 64
    assert limited == {
        "available": True,
        "tier": "B",
        "caveat": "Name is not saved",
        "reason": None,
        "status": "experimental",
    }
    assert unverified == {
        "available": False,
        "tier": "C",
        "caveat": None,
        "reason": "unreviewed_registry_entry",
        "status": "unavailable",
    }


@pytest.mark.contract("REL-IOC-009")
def test_absent_manifest_keeps_new_registered_format_behavior(monkeypatch):
    """Development extensions remain available when no artifact manifest is active."""
    from gwexpy_studio.ops import io

    monkeypatch.delenv("GWEXPY_STUDIO_IO_CAPABILITIES", raising=False)
    calls: list[dict[str, object]] = []

    class Native:
        @classmethod
        def read(cls, _source, *args, **kwargs):
            calls.append(kwargs)
            return "extension-result"

    monkeypatch.setattr(io, "io_class", lambda _datatype: Native)

    assert io.read_data("TimeSeries", "future.data", format="future_extension") == (
        "extension-result"
    )
    assert calls == [{"format": "future_extension"}]


@pytest.mark.contract("REL-IOC-015")
def test_absent_manifest_keeps_native_catalog_schema_unchanged(monkeypatch):
    """Development catalogs remain exactly native rather than gaining release fields."""
    from gwexpy_studio.ops import io

    monkeypatch.delenv("GWEXPY_STUDIO_IO_CAPABILITIES", raising=False)

    class Table(list[dict[str, str]]):
        colnames = ("Format", "Read", "Write", "Auto-identify")

    registry = SimpleNamespace(
        get_formats=lambda *_args: Table(
            [
                {
                    "Format": "native-extension",
                    "Read": "Yes",
                    "Write": "No",
                    "Auto-identify": "No",
                }
            ]
        )
    )
    monkeypatch.setattr(io, "io_class", lambda _datatype: object)
    monkeypatch.setattr(io, "_io_registries", lambda: (registry,))

    catalog = io.io_catalog("TimeSeries", "read")

    assert "capability_mode" not in catalog
    assert "capability_digest" not in catalog
    assert "capabilities" not in catalog["formats"][0]


@pytest.mark.contract("REL-IOC-010")
def test_auto_read_list_requires_each_source_to_identify_the_same_format(
    monkeypatch, tmp_path
):
    """One unreviewed source cannot make a multi-source native read proceed."""
    from gwexpy_studio.ops import io
    from gwexpy_studio.ops.io_capabilities import IOCapabilityError

    _activate_manifest(
        monkeypatch,
        tmp_path,
        [_entry("TimeSeries", "verified", "read", "A")],
    )
    _qualify_active_manifest()

    native_reads: list[object] = []

    class Native:
        @classmethod
        def read(cls, source, *_args, **_kwargs):
            native_reads.append(source)
            return "unexpected"

    registry_sources: list[str] = []

    def identify(_direction, _cls, path, *_args):
        registry_sources.append(path)
        return ["verified"] if path == "first.data" else ["unreviewed"]

    registry = SimpleNamespace(identify_format=identify)
    monkeypatch.setattr(io, "io_class", lambda _datatype: Native)
    monkeypatch.setattr(io, "_io_registries", lambda: (registry,))

    with pytest.raises(IOCapabilityError) as raised:
        io.read_data("TimeSeries", ["first.data", "second.data"])
    assert raised.value.reason == "frozen_unverified"
    assert registry_sources == ["first.data", "second.data"]
    assert native_reads == []


@pytest.mark.contract("REL-IOC-011")
def test_active_tier_b_write_reaches_native_value_api(monkeypatch, tmp_path):
    """A declared Tier B write remains usable while retaining its catalog caveat."""
    from gwexpy_studio.ops import io

    _activate_manifest(
        monkeypatch,
        tmp_path,
        [
            _entry(
                "TimeSeries",
                "limited",
                "write",
                "B",
                caveat="Name is not saved",
            )
        ],
    )
    calls: list[tuple[str, dict[str, object]]] = []
    NativeValue = type("TimeSeries", (), {})
    value = NativeValue()

    def write(target, **kwargs):
        calls.append((target, kwargs))

    value.write = write
    monkeypatch.setattr(io, "io_class", lambda _datatype: NativeValue)
    _qualify_active_manifest()

    assert io.write_data(value, "out.limited", format="limited") is None
    assert calls == [("out.limited", {"format": "limited"})]


@pytest.mark.contract("REL-IOC-012")
def test_active_tier_c_write_never_reaches_native_value_api(monkeypatch, tmp_path):
    """A blocked write direction is rejected before its native value can write."""
    from gwexpy_studio.ops import io
    from gwexpy_studio.ops.io_capabilities import IOCapabilityError

    _activate_manifest(
        monkeypatch,
        tmp_path,
        [
            _entry(
                "TimeSeries",
                "unavailable",
                "write",
                "C",
                reason="backend_not_bundled",
            )
        ],
    )
    NativeValue = type("TimeSeries", (), {})
    value = NativeValue()
    value.write = lambda *_args, **_kwargs: pytest.fail("native write was called")
    monkeypatch.setattr(io, "io_class", lambda _datatype: NativeValue)

    with pytest.raises(IOCapabilityError) as raised:
        io.write_data(value, "out.unavailable", format="unavailable")
    assert raised.value.code == "io_capability_unavailable"
    assert raised.value.reason == "backend_not_bundled"


@pytest.mark.contract("REL-IOC-013")
def test_active_write_without_an_explicit_format_fails_closed(monkeypatch, tmp_path):
    """A frozen writer never guesses a target format outside its direction policy."""
    from gwexpy_studio.ops import io
    from gwexpy_studio.ops.io_capabilities import IOCapabilityError

    _activate_manifest(
        monkeypatch,
        tmp_path,
        [_entry("TimeSeries", "verified", "write", "A")],
    )
    NativeValue = type("TimeSeries", (), {})
    value = NativeValue()
    value.write = lambda *_args, **_kwargs: pytest.fail("native write was called")
    monkeypatch.setattr(io, "io_class", lambda _datatype: NativeValue)

    with pytest.raises(IOCapabilityError) as raised:
        io.write_data(value, "out.auto")
    assert raised.value.reason == "frozen_unverified"


@pytest.mark.contract("REL-IOC-014")
def test_standalone_python_export_omits_artifact_capability_runtime():
    """Generated Python stays runnable without Studio's frozen-artifact module."""
    from gwexpy_studio.export.standalone_sources import native_helper_source

    source = native_helper_source()
    namespace: dict[str, object] = {"__name__": "__main__", "__package__": ""}
    exec(compile(source, "generated.py", "exec"), namespace)
    calls: list[dict[str, object]] = []

    class Native:
        @classmethod
        def read(cls, _source, *args, **kwargs):
            calls.append(kwargs)
            return "external-result"

    namespace["io_class"] = lambda _datatype: Native
    read_data = namespace["read_data"]
    assert callable(read_data)

    assert read_data("TimeSeries", "external.data", format="external") == (
        "external-result"
    )
    assert calls == [{"format": "external"}]


@pytest.mark.contract("REL-IOC-018")
def test_effective_snapshot_blocks_explicit_and_auto_reads_before_native_io(
    monkeypatch, tmp_path
):
    """A policy entry that vanished after worker probe cannot reach a native reader."""
    from gwexpy_studio.ops import io
    from gwexpy_studio.ops.io_capabilities import (
        IOCapabilityError,
        activate_effective_capability_snapshot,
        load_capability_manifest,
        probe_effective_capabilities,
    )

    _activate_manifest(
        monkeypatch,
        tmp_path,
        [_entry("TimeSeries", "gone", "read", "A")],
    )

    class EmptyTable(list[dict[str, str]]):
        colnames = ("Format", "Read", "Write", "Auto-identify")

    probed_registry = SimpleNamespace(get_formats=lambda *_args: EmptyTable())
    probed_io = SimpleNamespace(
        IO_CLASSES=("TimeSeries",),
        io_class=lambda _datatype: object,
        _io_registries=lambda: (probed_registry,),
    )
    snapshot = probe_effective_capabilities(
        load_capability_manifest(), io_module=probed_io
    )
    activate_effective_capability_snapshot(snapshot)
    calls: list[object] = []

    class Native:
        @classmethod
        def read(cls, source, *_args, **_kwargs):
            calls.append(source)
            return "unexpected"

    runtime_registry = SimpleNamespace(identify_format=lambda *_args: ["gone"])
    monkeypatch.setattr(io, "io_class", lambda _datatype: Native)
    monkeypatch.setattr(io, "_io_registries", lambda: (runtime_registry,))
    try:
        for explicit in ("gone", None):
            with pytest.raises(IOCapabilityError) as raised:
                io.read_data("TimeSeries", "input.data", format=explicit)
            assert raised.value.reason == "registry_missing"
        assert calls == []
    finally:
        activate_effective_capability_snapshot(None)


@pytest.mark.contract("REL-IOC-020")
def test_frozen_auto_identify_rejects_an_unreviewed_registry_candidate(
    monkeypatch, tmp_path
):
    """An auto route not in the available snapshot is never selected for a read."""
    from gwexpy_studio.ops import io
    from gwexpy_studio.ops.io_capabilities import (
        IOCapabilityError,
        activate_effective_capability_snapshot,
        load_capability_manifest,
        probe_effective_capabilities,
    )

    _activate_manifest(
        monkeypatch,
        tmp_path,
        [_entry("TimeSeries", "approved", "read", "A")],
    )

    class Table(list[dict[str, str]]):
        colnames = ("Format", "Read", "Write", "Auto-identify")

    probe_registry = SimpleNamespace(
        get_formats=lambda *_args: Table(
            [
                {
                    "Format": "approved",
                    "Read": "Yes",
                    "Write": "No",
                    "Auto-identify": "Yes",
                },
                {
                    "Format": "unreviewed",
                    "Read": "Yes",
                    "Write": "No",
                    "Auto-identify": "Yes",
                },
            ]
        ),
        get_reader=lambda *_args: object(),
    )
    probe_io = SimpleNamespace(
        IO_CLASSES=("TimeSeries",),
        io_class=lambda _datatype: object,
        _io_registries=lambda: (probe_registry,),
    )
    activate_effective_capability_snapshot(
        probe_effective_capabilities(load_capability_manifest(), io_module=probe_io)
    )
    native_calls: list[object] = []

    class Native:
        @classmethod
        def read(cls, source, *_args, **_kwargs):
            native_calls.append(source)
            return "unexpected"

    runtime_registry = SimpleNamespace(identify_format=lambda *_args: ["unreviewed"])
    monkeypatch.setattr(io, "io_class", lambda _datatype: Native)
    monkeypatch.setattr(io, "_io_registries", lambda: (runtime_registry,))
    try:
        with pytest.raises(IOCapabilityError) as raised:
            io.read_data("TimeSeries", "input.data")
        assert raised.value.reason == "frozen_unverified"
        assert native_calls == []
    finally:
        activate_effective_capability_snapshot(None)


@pytest.mark.contract("REL-IOC-021")
def test_frozen_auto_identify_without_an_available_auto_route_skips_registry(
    monkeypatch, tmp_path
):
    """An available read that is not auto-identifiable cannot query a registry."""
    from gwexpy_studio.ops import io
    from gwexpy_studio.ops.io_capabilities import (
        IOCapabilityError,
        activate_effective_capability_snapshot,
        load_capability_manifest,
        probe_effective_capabilities,
    )

    _activate_manifest(
        monkeypatch,
        tmp_path,
        [_entry("TimeSeries", "approved", "read", "A")],
    )

    class Table(list[dict[str, str]]):
        colnames = ("Format", "Read", "Write", "Auto-identify")

    probe_registry = SimpleNamespace(
        get_formats=lambda *_args: Table(
            [
                {
                    "Format": "approved",
                    "Read": "Yes",
                    "Write": "No",
                    "Auto-identify": "No",
                }
            ]
        ),
        get_reader=lambda *_args: object(),
    )
    probe_io = SimpleNamespace(
        IO_CLASSES=("TimeSeries",),
        io_class=lambda _datatype: object,
        _io_registries=lambda: (probe_registry,),
    )
    activate_effective_capability_snapshot(
        probe_effective_capabilities(load_capability_manifest(), io_module=probe_io)
    )
    monkeypatch.setattr(io, "io_class", lambda _datatype: object)
    monkeypatch.setattr(
        io,
        "_io_registries",
        lambda: pytest.fail("frozen auto identification queried a native registry"),
    )
    try:
        with pytest.raises(IOCapabilityError) as raised:
            io.identify_io("TimeSeries", "input.data")
        assert raised.value.reason == "frozen_unverified"
    finally:
        activate_effective_capability_snapshot(None)


@pytest.mark.contract("REL-IOC-019")
def test_frozen_io_without_worker_snapshot_fails_closed_before_native_write(
    monkeypatch, tmp_path
):
    """A static policy is not itself runtime qualification after worker isolation."""
    from gwexpy_studio.ops import io
    from gwexpy_studio.ops.io_capabilities import IOCapabilityError

    _activate_manifest(
        monkeypatch,
        tmp_path,
        [_entry("TimeSeries", "verified", "write", "A")],
    )
    NativeValue = type("TimeSeries", (), {})
    value = NativeValue()
    value.write = lambda *_args, **_kwargs: pytest.fail("native write was called")
    monkeypatch.setattr(io, "io_class", lambda _datatype: NativeValue)

    with pytest.raises(IOCapabilityError) as raised:
        io.write_data(value, "out.verified", format="verified")

    assert raised.value.reason == "probe_failed"
