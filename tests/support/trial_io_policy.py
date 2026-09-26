"""Approved read-route mirror for the current trial capability contract."""

from __future__ import annotations

from typing import Any

# GWexpy 0.2.0's DiagGUI frequency readers fail in both direct and adapter
# paths; keep those reviewed names unavailable until upstream support is
# released and verified.
TRIAL_IO_POLICY_PAIRS: dict[tuple[str, str], tuple[str, str | None]] = {
    ("TimeSeries", "csv"): ("A", None),
    ("TimeSeries", "gwf.lalframe"): ("A", None),
    ("TimeSeries", "gwf"): ("A", None),
    ("TimeSeriesDict", "gwf.lalframe"): ("A", None),
    ("TimeSeriesDict", "gwf"): ("A", None),
    ("TimeSeries", "xml.diaggui"): ("A", None),
    ("TimeSeriesDict", "xml.diaggui"): ("A", None),
    ("TimeSeriesMatrix", "xml.diaggui"): ("A", None),
    ("FrequencySeries", "xml.diaggui"): ("C", "native_error"),
    ("FrequencySeriesDict", "xml.diaggui"): ("C", "native_error"),
    ("FrequencySeriesMatrix", "xml.diaggui"): ("C", "native_error"),
    ("TimeSeries", "hdf.ndscope"): ("A", None),
    ("TimeSeriesDict", "hdf.ndscope"): ("A", None),
}


def trial_io_policy_document() -> dict[str, Any]:
    """Return the static policy in canonical pair order."""
    entries = []
    for (datatype, format_name), (tier, reason) in sorted(
        TRIAL_IO_POLICY_PAIRS.items()
    ):
        entry = {
            "datatype": datatype,
            "format": format_name,
            "direction": "read",
            "tier": tier,
        }
        if reason is not None:
            entry["reason"] = reason
        entries.append(entry)
    return {"schema_version": 1, "entries": entries}
