#!/usr/bin/env python3
"""Compare public native I/O and Studio delegation using real files, without SHM.

Run with ``PYTHONPATH=src .venv/bin/python scripts/verify_signal_io.py`` and
explicit fixture and output directories.
Optional readers remain failures or untested cases when their dependency or a
usable fixture is absent. Registry membership alone is never a successful case.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import importlib.metadata
import json
import platform
import signal
import tempfile
import time
import warnings
from collections import Counter
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from gwexpy_studio.ops.io import IO_CLASSES, io_catalog, read_data, write_data

EXTRA_FORMATS = ("hdf5", "csv", "csv_enhanced", "txt", "npy", "npz", "wav", "gwf")
ALIASES = {
    "dttxml": "xml.diaggui",
    "frame": "gwf",
    "framecpp": "gwf",
    "framel": "gwf",
    "lalframe": "gwf",
    "gwf.framecpp": "gwf",
    "gwf.framel": "gwf",
    "gwf.lalframe": "gwf",
    "miniseed": "mseed",
    "nc": "netcdf4",
    "ndscope-hdf5": "hdf.ndscope",
    "ndscope_hdf5": "hdf.ndscope",
    "ndscopehdf5": "hdf.ndscope",
    "win32": "win",
    "ats.mth5": "ats",
}
FIXTURES = {
    "xml.diaggui": "diaggui.xml",
    "hdf.ndscope": "ndscope.h5",
    "sdb": "davis.db",
    "netcdf4": "test.nc",
    **{
        fmt: f"test.{fmt}"
        for fmt in (
            "ats",
            "csv",
            "flac",
            "gbd",
            "gwf",
            "mp3",
            "mseed",
            "npy",
            "npz",
            "ogg",
            "tdms",
            "txt",
            "wav",
            "win",
            "zarr",
        )
    },
}
CALL_TIMEOUT_S = 15
_NATIVE_INITIALIZED = False


def _native_class(name: str) -> type:
    import gwexpy

    global _NATIVE_INITIALIZED
    if not _NATIVE_INITIALIZED:
        gwexpy.register_all()
        _NATIVE_INITIALIZED = True
    family = next(
        item
        for item in ("TimeSeries", "FrequencySeries", "Spectrogram")
        if name.startswith(item)
    )
    return getattr(importlib.import_module("gwexpy." + family.lower()), name)


def inventory() -> dict[str, dict[str, list[str]]]:
    """Snapshot candidates and explicitly expose probes outside the registries."""
    result = {}
    for name in IO_CLASSES:
        item = {}
        for direction in ("read", "write"):
            registered = sorted(
                row["format"] for row in io_catalog(name, direction)["formats"]
            )
            item[direction] = registered
            item[f"direct_extra_{direction}"] = sorted(
                set(EXTRA_FORMATS) - set(registered)
            )
        result[name] = item
    return result


def native_sample(name: str) -> Any:
    """Create small, named, regular native objects with nontrivial numeric data."""
    family = next(
        item
        for item in ("TimeSeries", "FrequencySeries", "Spectrogram")
        if name.startswith(item)
    )
    cls, single = _native_class(name), _native_class(family)
    shape = (8, 16) if family == "Spectrogram" else (128,)
    data = np.sin(np.arange(128) * 0.13).reshape(shape)
    axes: dict[str, Any] = {
        "TimeSeries": {"t0": 1000000000, "dt": 1 / 8000},
        "FrequencySeries": {"f0": 1, "df": 0.5},
        "Spectrogram": {"t0": 1000000000, "dt": 0.125, "f0": 1, "df": 0.5},
    }[family]
    leaves = [
        single(
            data * (i + 1),
            unit="m",
            name=f"X1:CHANNEL_{i}",
            channel=f"X1:CHANNEL_{i}",
            **axes,
        )
        for i in range(2)
    ]
    if name.endswith("Dict"):
        return cls({leaf.name: leaf for leaf in leaves})
    if name.endswith("List"):
        return cls(leaves)
    if name.endswith("Matrix"):
        if family == "Spectrogram":
            from gwexpy.types.metadata import MetaDataMatrix

            axes = {
                "times": leaves[0].times,
                "frequencies": leaves[0].frequencies,
                "epoch": leaves[0].epoch,
                "meta": MetaDataMatrix(
                    [
                        [{"unit": "m", "name": leaf.name, "channel": leaf.channel}]
                        for leaf in leaves
                    ]
                ),
            }
        return cls(
            np.stack([leaf.value for leaf in leaves])[:, None],
            units=[["m"], ["m"]],
            names=[[leaf.name] for leaf in leaves],
            rows=["r0", "r1"],
            cols=["c0"],
            **axes,
        )
    return leaves[0]


def _array(value: Any) -> dict[str, Any]:
    array = np.asarray(value)
    return {
        "shape": list(array.shape),
        "dtype": str(array.dtype),
        "sha256": hashlib.sha256(np.ascontiguousarray(array).tobytes()).hexdigest(),
    }


def snapshot(value: Any) -> dict[str, Any]:
    """Capture native class, exact data/coordinate bytes and scientific labels."""
    name = type(value).__name__
    result: dict[str, Any] = {"class": name}
    if isinstance(value, Mapping):
        result["members"] = [
            {"key": key, "value": snapshot(member)} for key, member in value.items()
        ]
    elif name.endswith("List"):
        result["members"] = [snapshot(member) for member in value]
    else:
        result["values"] = _array(value.value)
        for field in ("unit", "name", "channel", "epoch"):
            result[field] = (
                None
                if getattr(value, field, None) is None
                else str(getattr(value, field))
            )
        for field in ("times", "frequencies"):
            if hasattr(value, field):
                coordinate = getattr(value, field)
                result[field] = (
                    None
                    if coordinate is None
                    else {
                        **_array(coordinate.value),
                        "unit": str(coordinate.unit),
                    }
                )
        if name.endswith("Matrix"):
            for field in ("units", "names", "channels", "rows", "cols"):
                result[field] = (
                    np.asarray(getattr(value, field, [])).astype(str).tolist()
                )
    return result


def _error_category(exc: Exception) -> str:
    message = str(exc).lower()
    if isinstance(exc, (ImportError, ModuleNotFoundError)) or any(
        text in message
        for text in (
            "missing optional dependency",
            "no module named",
            "not installed",
        )
    ):
        return "missing_dependency"
    if isinstance(exc, NotImplementedError) or any(
        text in message
        for text in (
            "no reader defined",
            "no writer defined",
            "unsupported format",
            "unknown format",
            "not supported for format",
            "unrecognized format",
        )
    ):
        return "native_unimplemented"
    return "timeout" if isinstance(exc, TimeoutError) else "native_error"


def _expired(*_: Any) -> None:
    raise TimeoutError(f"Native I/O exceeded {CALL_TIMEOUT_S}s")


def attempt(call: Callable[[], Any], path: Path, *, read: bool) -> dict[str, Any]:
    """Record actual calls and diagnostics; exceptions are evidence, not skips."""
    started = time.monotonic()
    result: dict[str, Any] = {"attempted": True, "path": str(path)}
    previous = signal.signal(signal.SIGALRM, _expired)
    signal.setitimer(signal.ITIMER_REAL, CALL_TIMEOUT_S)
    try:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            value = call()
            result.update(
                status="success",
                result=snapshot(value) if read else {"path_exists": path.exists()},
            )
    except Exception as exc:
        result.update(
            status=_error_category(exc),
            error_type=type(exc).__name__,
            error=str(exc)[:6000],
        )
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
        result["duration_s"] = round(time.monotonic() - started, 6)
    result["warnings"] = [str(item.message) for item in caught]
    return result


def _direct_read(name: str, path: Path, fmt: str, kwargs: dict[str, Any]) -> Any:
    cls = _native_class(name)
    if name in ("SpectrogramDict", "SpectrogramList"):
        value = cls()
        result = value.read(str(path), format=fmt, **kwargs)
        return value if result is None else result
    return getattr(cls, "read")(str(path), format=fmt, **kwargs)


def _pair(
    direct: dict[str, Any], adapter: dict[str, Any], *, write: bool = False
) -> dict[str, Any]:
    same = direct["status"] == adapter["status"]
    if same and direct["status"] == "success":
        same = direct["result"] == adapter["result"]
    elif same:
        same = direct.get("error_type") == adapter.get("error_type")
    status = (
        ("equivalent_success" if direct["status"] == "success" else direct["status"])
        if same
        else "adapter_mismatch"
    )
    if write and status == "equivalent_success" and not direct["result"]["path_exists"]:
        status = "native_write_no_artifact"
    return {
        "status": status,
        "adapter_matches_native": same,
        "direct": direct,
        "adapter": adapter,
    }


def _reader_options(name: str, fmt: str) -> dict[str, Any]:
    canonical = ALIASES.get(fmt, fmt)
    if canonical == "xml.diaggui":
        return {
            "products": "TF"
            if name.endswith("Matrix") and name.startswith("Frequency")
            else "ASD"
            if name.startswith("Frequency")
            else "TS"
        }
    return {"timezone": "UTC"} if canonical == "gbd" else {}


def _suffix(fmt: str) -> str:
    canonical = ALIASES.get(fmt, fmt)
    return {
        "hdf5": "h5",
        "hdf.ndscope": "h5",
        "netcdf4": "nc",
        "csv_enhanced": "csv",
    }.get(canonical, canonical.replace(".", "_"))


def _fingerprint(path: Path) -> list[dict[str, Any]]:
    files = (
        sorted(item for item in path.rglob("*") if item.is_file())
        if path.is_dir()
        else [path]
    )
    return [
        {
            "name": str(item.relative_to(path)) if path.is_dir() else path.name,
            "bytes": item.stat().st_size,
            "sha256": hashlib.sha256(item.read_bytes()).hexdigest(),
        }
        for item in files
    ]


def run_case(
    name: str, fmt: str, work: Path, fixtures: Path, *, write: bool = True
) -> dict[str, Any]:
    """Run a pair of writers and readers, preferring a native-written fixture."""
    case: dict[str, Any] = {
        "datatype": name,
        "format": fmt,
        "args": [],
        "read_kwargs": _reader_options(name, fmt),
        "write_kwargs": {},
    }
    case_dir = work / name / fmt.replace(".", "_")
    direct_path = case_dir / "direct" / ("sample." + _suffix(fmt))
    adapter_path = case_dir / "adapter" / direct_path.name
    kwargs = case["read_kwargs"]
    source = None
    unchanged = []
    if write:
        direct_path.parent.mkdir(parents=True, exist_ok=True)
        adapter_path.parent.mkdir(parents=True, exist_ok=True)
        native = native_sample(name)
        adapted = native_sample(name)
        before = snapshot(native)
        direct = attempt(
            lambda: native.write(str(direct_path), format=fmt), direct_path, read=False
        )
        unchanged.append(snapshot(native) == before)
        adapter = attempt(
            lambda: write_data(adapted, str(adapter_path), format=fmt),
            adapter_path,
            read=False,
        )
        unchanged.append(snapshot(adapted) == before)
        case["write"] = _pair(direct, adapter, write=True)
        if direct["status"] == "success" and direct_path.exists():
            source = direct_path
            case["fixture_origin"] = "generated_native_write"
            first = attempt(
                lambda: _direct_read(name, direct_path, fmt, kwargs),
                direct_path,
                read=True,
            )
            second = attempt(
                lambda: _direct_read(name, adapter_path, fmt, kwargs),
                adapter_path,
                read=True,
            )
            case["write"]["readback"] = _pair(first, second)
            case["write"]["roundtrip_exact"] = (
                first.get("result") == before if first["status"] == "success" else None
            )
            case["write"]["roundtrip_different_fields"] = (
                [
                    key
                    for key in before
                    if first.get("result", {}).get(key) != before[key]
                ]
                if first["status"] == "success"
                else []
            )
            if case["write"]["readback"]["status"] == "adapter_mismatch":
                case["write"]["status"] = "adapter_mismatch"
            elif case["write"]["readback"]["status"] != "equivalent_success":
                case["write"]["status"] = "write_success_comparison_unavailable"
    else:
        case["write"] = {"status": "not_requested"}
    if source is None:
        candidate = FIXTURES.get(ALIASES.get(fmt, fmt))
        if candidate and (fixtures / candidate).exists():
            source = fixtures / candidate
            case["fixture_origin"] = "upstream_existing"
    if source is None:
        case["read"] = {
            "status": "fixture_missing",
            "direct": {"attempted": False},
            "adapter": {"attempted": False},
        }
        case["fixture_search"] = str(fixtures)
    else:
        case["fixture_files"] = _fingerprint(source)
        case["read"] = _pair(
            attempt(lambda: _direct_read(name, source, fmt, kwargs), source, read=True),
            attempt(
                lambda: read_data(name, str(source), format=fmt, kwargs=kwargs),
                source,
                read=True,
            ),
        )
    case["source_unchanged"] = all(unchanged) if unchanged else None
    return case


def _versions() -> dict[str, str]:
    result = {"python": platform.python_version()}
    for package in (
        "gwexpy",
        "gwpy",
        "numpy",
        "astropy",
        "h5py",
        "soundfile",
        "pydub",
        "netCDF4",
        "zarr",
        "obspy",
        "nptdms",
        "mth5",
        "dttxml",
        "uproot",
    ):
        try:
            result[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            result[package] = "not installed"
    return result


def _markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Native I/O comparison evidence",
        "",
        f"Generated: {report['generated_at']}",
        "",
        "The JSON file records every candidate, actual call, error, warnings, "
        "and exact scientific snapshot. Successful read comparisons use the "
        "same real file. Writer output is compared by native readback where "
        "available. Missing fixtures, native errors, and missing dependencies "
        "are not successes.",
        "",
        "| Class | Registered read/write | Extra read/write probes | "
        "Read equivalent | Write equivalent |",
        "|---|---:|---:|---:|---:|",
    ]
    for name, item in report["inventory"].items():
        cases = [case for case in report["cases"] if case["datatype"] == name]
        successes = {
            direction: sum(
                case[direction]["status"] == "equivalent_success" for case in cases
            )
            for direction in ("read", "write")
        }
        lines.append(
            f"| {name} | {len(item['read'])}/{len(item['write'])} | "
            f"{len(item['direct_extra_read'])}/"
            f"{len(item['direct_extra_write'])} | "
            f"{successes['read']} | {successes['write']} |"
        )
    lines += ["", "| Outcome | Read cases | Write cases |", "|---|---:|---:|"]
    for status in sorted(
        set(report["totals"]["read"]) | set(report["totals"]["write"])
    ):
        lines.append(
            f"| {status} | {report['totals']['read'].get(status, 0)} | "
            f"{report['totals']['write'].get(status, 0)} |"
        )
    lines += [
        "",
        "Native roundtrip differences (exact dtype, values, coordinates, "
        "units and labels are compared):",
        "",
        "| Class | Format | Different scientific fields |",
        "|---|---|---|",
    ]
    for case in report["cases"]:
        if case["write"].get("roundtrip_exact") is False:
            fields = ", ".join(case["write"]["roundtrip_different_fields"])
            lines.append(f"| {case['datatype']} | {case['format']} | {fields} |")
    lines += [
        "",
        "Limits: these fixtures cover small regular float64 objects. Strict "
        "roundtrip differences include native format metadata loss; they are "
        "separate from adapter equivalence. Read-only formats without local "
        "upstream fixtures remain unverified. Some existing upstream fixtures "
        "are placeholders; their actual parse errors are retained. A registered "
        "candidate is not a claim that its backend is installed. No dependencies "
        "were installed by this verifier. Each native call has a 15-second "
        "timeout. No shared memory, worker RPC, or GUI is used.",
        "",
        "Separate store regression tests cover SpectrogramMatrix member browsing "
        "when matrix epoch differs from the first explicit time. The store "
        "constructs the selected public Spectrogram view from native arrays and "
        "cell metadata. This does not duplicate a format reader or repair native "
        "HDF5 metadata loss; the native I/O results above retain that limitation.",
        "",
        "Reproduce:",
        "",
        "```sh",
        "PYTHONPATH=src .venv/bin/python scripts/verify_signal_io.py "
        "--fixtures /path/to/fixtures --output /path/to/io-comparison",
        "```",
        "",
    ]
    lines += [
        "Every candidate (R/W flags indicate registry membership):",
        "",
        "| Class | Format | Registered | Read | Write | Exact native roundtrip |",
        "|---|---|---|---|---|---|",
    ]
    for case in report["cases"]:
        flags = "/".join(
            "yes" if case["registered"][direction] else "no"
            for direction in ("read", "write")
        )
        exact = case["write"].get("roundtrip_exact")
        exact_text = "unavailable" if exact is None else "yes" if exact else "no"
        lines.append(
            f"| {case['datatype']} | {case['format']} | {flags} | "
            f"{case['read']['status']} | {case['write']['status']} | "
            f"{exact_text} |"
        )
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    """Write the complete registry and direct-call evidence matrix."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fixtures",
        type=Path,
        required=True,
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    catalog = inventory()
    report: dict[str, Any] = {
        "generated_at": datetime.now(UTC).isoformat(),
        "environment": _versions(),
        "fixture_root": str(args.fixtures.resolve()),
        "inventory": catalog,
        "cases": [],
    }
    with tempfile.TemporaryDirectory(prefix="studio-native-io-") as temp:
        for name, formats in catalog.items():
            write_formats = set(formats["write"]) | set(formats["direct_extra_write"])
            for fmt in sorted(
                set(formats["read"]) | set(formats["direct_extra_read"]) | write_formats
            ):
                case = run_case(
                    name, fmt, Path(temp), args.fixtures, write=fmt in write_formats
                )
                case["registered"] = {
                    direction: fmt in formats[direction]
                    for direction in ("read", "write")
                }
                report["cases"].append(case)
            print(name, len(report["cases"]), "cumulative cases", flush=True)
    report["totals"] = {
        direction: dict(Counter(case[direction]["status"] for case in report["cases"]))
        for direction in ("read", "write")
    }
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "io-matrix.json").write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8"
    )
    (args.output / "io-matrix.md").write_text(_markdown(report), encoding="utf-8")
    print(json.dumps(report["totals"], sort_keys=True), flush=True)
    return int(
        any(
            case[direction]["status"] == "adapter_mismatch"
            for case in report["cases"]
            for direction in ("read", "write")
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
