"""Format-agnostic delegation to the public GWexpy class I/O entry points."""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from typing import Any

from .values import decode_value

IO_CLASSES = tuple(
    family + suffix
    for family in ("TimeSeries", "FrequencySeries", "Spectrogram")
    for suffix in ("", "Dict", "List", "Matrix")
)
_IO_INITIALIZED = False
_IO_RESERVED = frozenset({"source", "target", "format", "args", "kwargs", "datatype"})


def io_class(class_name: str) -> type:
    """Resolve only the twelve supported public classes, registering native I/O."""
    import importlib

    import gwexpy

    global _IO_INITIALIZED
    if class_name not in IO_CLASSES:
        raise ValueError(f"Unsupported data class: {class_name!r}")
    if not _IO_INITIALIZED:
        gwexpy.register_all()
        _IO_INITIALIZED = True
    package = (
        "timeseries"
        if class_name.startswith("TimeSeries")
        else (
            "frequencyseries"
            if class_name.startswith("FrequencySeries")
            else "spectrogram"
        )
    )
    return getattr(importlib.import_module(f"gwexpy.{package}"), class_name)


def _io_registries() -> tuple[Any, Any]:
    from astropy.io import registry
    from gwpy.io.registry import default_registry

    return default_registry, registry


def _capability_manifest() -> Any | None:
    """Return worker runtime qualification, never a GUI-side backend probe."""
    if not __package__:
        # ``native_helper_source`` embeds this module into a generated Python
        # script.  That script deliberately has no Studio package or artifact
        # contract, so it retains public GWexpy development-mode behavior.
        return None
    from .io_capabilities import (
        current_effective_capability_snapshot,
        load_capability_manifest,
        unprobed_effective_capabilities,
    )

    snapshot = current_effective_capability_snapshot()
    if snapshot is not None:
        return None if snapshot.mode == "developer" else snapshot
    manifest = load_capability_manifest(io_classes=IO_CLASSES)
    return (
        None
        if manifest.mode == "developer"
        else unprobed_effective_capabilities(manifest)
    )


def _effective_io_catalog(
    snapshot: Any, *, datatype: str, direction: str
) -> dict[str, Any]:
    """Present exactly the worker-probed snapshot without touching native I/O."""
    import gwexpy

    from .io_capabilities import capability_annotation

    formats: dict[str, dict[str, Any]] = {}
    for entry in snapshot.entries:
        if entry.datatype != datatype:
            continue
        item = formats.setdefault(
            entry.format,
            {
                "format": entry.format,
                "read": False,
                "write": False,
                "auto_identify": False,
            },
        )
        item[entry.direction] = item[entry.direction] or entry.native_available
        if entry.direction == "read":
            item["auto_identify"] = item["auto_identify"] or entry.auto_identify
    for name, item in formats.items():
        item["capabilities"] = {
            candidate_direction: capability_annotation(
                snapshot,
                datatype=datatype,
                format_name=name,
                direction=candidate_direction,
                native_available=bool(item[candidate_direction]),
            )
            for candidate_direction in ("read", "write")
        }
    return {
        "classes": list(IO_CLASSES),
        "datatype": datatype,
        "direction": direction,
        "formats": [formats[key] for key in sorted(formats)],
        "gwexpy_version": str(gwexpy.__version__),
        "catalog_version": 1,
        "capability_mode": snapshot.mode,
        "capability_digest": snapshot.policy_digest,
        "capability_effective_digest": snapshot.digest,
    }


def io_catalog(datatype: str = "TimeSeries", direction: str = "read") -> dict[str, Any]:
    """Return native registry candidates, not a promise of installed availability."""
    import gwexpy

    from .io_capabilities import EffectiveCapabilitySnapshot, capability_annotation

    if direction not in ("read", "write"):
        raise ValueError("I/O direction must be read or write")
    manifest = _capability_manifest()
    if isinstance(manifest, EffectiveCapabilitySnapshot):
        return _effective_io_catalog(manifest, datatype=datatype, direction=direction)
    cls = io_class(datatype)
    formats: dict[str, dict[str, Any]] = {}
    for registry in _io_registries():
        table = registry.get_formats(cls, direction.title())
        for row in table:
            name = str(row["Format"])
            item = formats.setdefault(
                name,
                {"format": name, "read": False, "write": False, "auto_identify": False},
            )
            for column, key in (
                ("Read", "read"),
                ("Write", "write"),
                ("Auto-identify", "auto_identify"),
            ):
                if column in table.colnames:
                    item[key] = item[key] or str(row[column]).lower() in (
                        "yes",
                        "true",
                        "1",
                    )
    if manifest is not None:
        for name, item in formats.items():
            item["capabilities"] = {
                candidate_direction: capability_annotation(
                    manifest,
                    datatype=datatype,
                    format_name=name,
                    direction=candidate_direction,
                    native_available=bool(item[candidate_direction]),
                )
                for candidate_direction in ("read", "write")
            }
    return {
        "classes": list(IO_CLASSES),
        "datatype": datatype,
        "direction": direction,
        "formats": [formats[key] for key in sorted(formats)],
        "gwexpy_version": str(gwexpy.__version__),
        "catalog_version": 1,
        **(
            {
                "capability_mode": manifest.mode,
                "capability_digest": manifest.digest,
            }
            if manifest is not None
            else {}
        ),
    }


def _io_options(
    args: Sequence[Any] | None, kwargs: Mapping[str, Any] | None
) -> tuple[list[Any], dict[str, Any]]:
    if args is not None and not isinstance(args, (list, tuple)):
        raise ValueError("Positional I/O arguments must be an array")
    if kwargs is not None and not isinstance(kwargs, Mapping):
        raise ValueError("Keyword I/O arguments must be a mapping")
    encoded_args = list(args) if args is not None else []
    encoded_kwargs = dict(kwargs) if kwargs is not None else {}
    overlap = _IO_RESERVED.intersection(encoded_kwargs)
    if overlap:
        raise ValueError(f"I/O options override reserved fields: {sorted(overlap)}")
    return decode_value(encoded_args), decode_value(encoded_kwargs)


def _available_auto_read_formats(snapshot: Any, datatype: str) -> frozenset[str]:
    """Return only worker-qualified read routes that permit auto-identification."""
    from .io_capabilities import IOCapabilityError

    read_entries = tuple(
        entry
        for entry in snapshot.entries
        if entry.datatype == datatype and entry.direction == "read"
    )
    formats = frozenset(
        entry.format
        for entry in read_entries
        if entry.available and entry.auto_identify
    )
    if not formats:
        reasons = {entry.reason for entry in read_entries if entry.reason is not None}
        raise IOCapabilityError(
            reason=next(iter(reasons)) if len(reasons) == 1 else "frozen_unverified"
        )
    return formats


def read_data(
    class_name: str,
    source: str | list[str],
    *,
    format: str | None = None,
    args: Sequence[Any] | None = None,
    kwargs: Mapping[str, Any] | None = None,
) -> Any:
    """Read through a native class method without per-format implementations."""
    if not (isinstance(source, str) and source) and not (
        isinstance(source, list)
        and source
        and all(isinstance(path, str) and path for path in source)
    ):
        raise ValueError("Choose a source or an ordered nonempty source list")
    call_args, call_kwargs = _io_options(args, kwargs)
    manifest = _capability_manifest()
    if format and manifest is not None:
        manifest.require(class_name, format, "read")
    auto_formats: frozenset[str] | None = None
    if not format and manifest is not None and manifest.active:
        auto_formats = _available_auto_read_formats(manifest, class_name)
    cls = io_class(class_name)
    if manifest is not None and auto_formats is not None:
        format = _identify_read_sources(
            cls,
            source,
            call_args,
            call_kwargs,
            allowed_formats=auto_formats,
        )
        manifest.require(class_name, format, "read")
    if format:
        call_kwargs["format"] = format
    if class_name in ("SpectrogramDict", "SpectrogramList"):
        instance = cls()
        result = instance.read(source, *call_args, **call_kwargs)
        return instance if result is None else result
    return getattr(cls, "read")(source, *call_args, **call_kwargs)


def write_data(
    value: Any,
    target: str,
    *,
    format: str | None = None,
    args: Sequence[Any] | None = None,
    kwargs: Mapping[str, Any] | None = None,
) -> None:
    """Write the native object using exactly the user-selected format and options."""
    datatype = type(value).__name__
    manifest = _capability_manifest()
    if manifest is not None and manifest.active:
        if format:
            manifest.require(datatype, format, "write")
        else:
            # Unlike a read, the public registry has no stable write-format
            # resolver for an arbitrary new target.  Guessing would permit an
            # unverified format, so packaged builds require an explicit entry.
            from .io_capabilities import IOCapabilityError

            raise IOCapabilityError(reason="frozen_unverified")
    io_class(datatype)
    call_args, call_kwargs = _io_options(args, kwargs)
    if format:
        call_kwargs["format"] = format
    value.write(target, *call_args, **call_kwargs)


def identify_io(
    datatype: str,
    source: str,
    *,
    args: Sequence[Any] | None = None,
    kwargs: Mapping[str, Any] | None = None,
) -> str:
    """Resolve a unique native registry format or request an explicit selection."""
    call_args, call_kwargs = _io_options(args, kwargs)
    manifest = _capability_manifest()
    auto_formats: frozenset[str] | None = None
    if manifest is not None and manifest.active:
        auto_formats = _available_auto_read_formats(manifest, datatype)
    cls = io_class(datatype)
    format_name = _identify_read_sources(
        cls,
        source,
        call_args,
        call_kwargs,
        allowed_formats=auto_formats,
    )
    if manifest is not None:
        manifest.require(datatype, format_name, "read")
    return format_name


def _identify_read_sources(
    cls: type,
    source: str | list[str],
    call_args: Sequence[Any],
    call_kwargs: Mapping[str, Any],
    *,
    allowed_formats: Collection[str] | None = None,
) -> str:
    """Resolve one unique reader format across one source or ordered source list."""
    sources = [source] if isinstance(source, str) else source
    allowed = frozenset(allowed_formats) if allowed_formats is not None else None
    found: set[str] = set()
    for path in sources:
        candidates: set[str] = set()
        for registry in _io_registries():
            candidates.update(
                registry.identify_format(
                    "read", cls, path, None, [path, *call_args], dict(call_kwargs)
                )
            )
        if allowed is not None:
            candidates.intersection_update(allowed)
        if len(candidates) != 1:
            if not candidates and allowed is not None:
                from .io_capabilities import IOCapabilityError

                raise IOCapabilityError(reason="frozen_unverified")
            raise _automatic_selection_error(candidates)
        found.update(candidates)
    if len(found) != 1:
        if not found and allowed is not None:
            from .io_capabilities import IOCapabilityError

            raise IOCapabilityError(reason="frozen_unverified")
        raise _automatic_selection_error(found)
    return found.pop()


def _automatic_selection_error(found: set[str]) -> ValueError:
    """Build the existing public auto-identification failure without path data."""
    return ValueError(
        "Select a format explicitly: native automatic identification is "
        + (
            "ambiguous (" + ", ".join(sorted(found)) + ")"
            if found
            else "unavailable"
        )
    )
