"""Signal and native I/O RPCs sharing the worker's transfer lifecycle."""

from __future__ import annotations

import time
import warnings
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

import numpy as np

from ..domain.project_v2 import record_dict
from ..ops.intake import compact_inspection, inspect_io, inspection_sha256
from ..ops.io import io_catalog, write_data
from ..ops.native_filters import (
    SCIENCE_FILTER_NAMES,
    filter_response,
    filter_response_from_recipes,
)
from ..ops.registry import REGISTRY
from ..ops.source_fingerprint import fingerprint_sources
from ..ops.spec import normalize_params
from ..runtime.store import ObjectStore
from .shm import create_block

SIGNAL_REQUESTS = frozenset(
    {
        "catalog_io",
        "inspect_io",
        "list_members",
        "preview_data",
        "filter_preview",
        "write_data",
    }
)


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    return dict(value)


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a nonempty string")
    return value


class SignalService:
    """Serve native metadata, previews, filter responses, and external writes."""

    def __init__(self, objects: ObjectStore, transfers: Any) -> None:
        """Reuse the existing worker object and pending-block stores."""
        self.objects = objects
        self.transfers = transfers

    def dispatch(self, request_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Run an RPC and roll back any partially created transfer on failure."""
        before = set(self.transfers.pending)
        try:
            return self._dispatch(request_type, payload)
        except BaseException:
            for name in set(self.transfers.pending) - before:
                self.transfers.release_shm(name)
            raise

    def _dispatch(self, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        if kind == "catalog_io":
            return {
                "io_catalog": io_catalog(
                    payload.get("datatype", "TimeSeries"),
                    payload.get("direction", "read"),
                )
            }
        if kind == "inspect_io":
            if payload.get("mode") == "fingerprint":
                manifest = fingerprint_sources(
                    _mapping(payload.get("request"), "request")
                )
                if (
                    "expected_manifest" in payload
                    and manifest != payload["expected_manifest"]
                ):
                    raise ValueError("Source content or ordered inventory changed")
                return {"source_manifest": manifest}
            inspected = inspect_io(_mapping(payload.get("request"), "request"))
            if ("expected" in payload and inspected != payload["expected"]) or (
                "expected_sha256" in payload
                and inspection_sha256(inspected) != payload["expected_sha256"]
            ):
                raise ValueError(
                    "Selected source changed after inspection; "
                    "inspect and confirm again"
                )
            if "expected_sha256" in payload:
                return {"io_inspection": {"validated": True}}
            if payload.get("compact") is True:
                return compact_inspection(inspected)
            return {"io_inspection": inspected}
        if kind == "list_members":
            object_id = _text(payload.get("object_id"), "object_id")
            members = self.objects.list_members(
                object_id,
                offset=payload.get("offset", 0),
                limit=payload.get("limit", 100),
            )
            return {
                "object_id": object_id,
                "members": record_dict(members),
                "member_count": self.objects.describe(object_id).metadata.get(
                    "member_count", 0
                ),
            }
        if kind == "preview_data":
            value, ref = self._selected(payload)
            ref = replace(
                ref,
                axes={
                    key: {**axis, "unit": "s" if key == "time" else "Hz"}
                    if key in ("time", "frequency")
                    else axis
                    for key, axis in ref.axes.items()
                },
            )
            return {
                "object_ref": record_dict(ref),
                "arrays": self._arrays(value),
                "selector": payload.get("selector"),
            }
        if kind == "filter_preview":
            return self._filter_preview(payload)
        if kind == "write_data":
            return self._write(payload)
        raise ValueError(f"Unknown signal request: {kind}")

    def _selected(self, payload: Mapping[str, Any]) -> tuple[Any, Any]:
        object_id = _text(payload.get("object_id"), "object_id")
        parent = self.objects.describe(object_id)
        selector = payload.get("selector")
        if selector is None:
            return self.objects.get(object_id), parent
        selected = self.objects.get_member(object_id, _mapping(selector, "selector"))
        ref = ObjectStore().put(
            selected, object_id=object_id, produced_by=parent.produced_by
        )
        return selected, replace(
            ref,
            metadata={
                **ref.metadata,
                "parent_object_id": object_id,
                "member_selector": dict(selector),
            },
        )

    def _transfer(self, values: np.ndarray, unit: str) -> dict[str, Any]:
        block = create_block(np.asarray(values))
        self.transfers.pending.add(block.name)
        try:
            return {**record_dict(block.descriptor), "unit": unit}
        finally:
            block.shm.close()

    def _arrays(self, value: Any) -> dict[str, Any]:
        kind = type(value).__name__
        if kind not in ("TimeSeries", "FrequencySeries", "Spectrogram"):
            raise ValueError("Select a single native member for preview")
        arrays: dict[str, Any] = {
            "values": self._transfer(value.value, str(value.unit)),
            "x_coordinates": None,
            "y_coordinates": None,
        }
        for key, prop, default_unit in (
            (
                "x_coordinates",
                "frequencies" if kind == "FrequencySeries" else "times",
                "Hz" if kind == "FrequencySeries" else "s",
            ),
            ("y_coordinates", "frequencies", "Hz"),
        ):
            if key == "y_coordinates" and kind != "Spectrogram":
                continue
            coordinates = getattr(value, prop)
            if hasattr(coordinates, "unit") and str(coordinates.unit) != default_unit:
                coordinates = coordinates.to(default_unit)
            arrays[key] = self._transfer(
                np.asarray(getattr(coordinates, "value", coordinates)), default_unit
            )
        return arrays

    def _filter_preview(self, payload: dict[str, Any]) -> dict[str, Any]:
        from gwexpy.frequencyseries import FrequencySeries

        operation = _text(payload.get("op_name"), "op_name")
        spec = REGISTRY.get(operation)
        if (
            spec is None
            or operation.removeprefix("timeseries.") not in SCIENCE_FILTER_NAMES
        ):
            raise ValueError(f"Unknown operation {operation!r}")
        raw_inputs = _mapping(payload.get("inputs"), "inputs")
        inputs = {
            role: self._selected(_mapping(source, f"inputs.{role}"))[0]
            for role, source in raw_inputs.items()
        }
        params = normalize_params(
            _mapping(payload.get("params", {}), "params"), spec=spec
        )
        recorded = payload.get("recorded_details")
        response = (
            filter_response_from_recipes(_mapping(recorded, "recorded_details"))
            if recorded is not None
            else filter_response(operation, inputs, params)
        )
        members = []
        for member in response["members"]:
            series = FrequencySeries(
                member["response"],
                frequencies=member["frequency"],
                unit=member.get("unit", ""),
                name=member["label"],
            )
            reference = ObjectStore().put(series, object_id="preview-filter")
            members.append(
                {
                    "label": member["label"],
                    "recipe": member["recipe"],
                    "object_ref": record_dict(reference),
                    "arrays": self._arrays(series),
                }
            )
        return {
            "generation": payload.get("generation"),
            "description": response.get("description", ""),
            "members": members,
        }

    def _write(self, payload: dict[str, Any]) -> dict[str, Any]:
        value, _ = self._selected(payload)
        request = _mapping(payload.get("request"), "request")
        raw_target = request.get("target")
        if "paths" in request:
            paths = request["paths"]
            if not isinstance(paths, list) or len(paths) != 1:
                raise ValueError("Writing requires exactly one target path")
            if raw_target is not None and raw_target != paths[0]:
                raise ValueError("Conflicting target and paths in write request")
            raw_target = paths[0]
        target = Path(_text(raw_target, "target")).expanduser().absolute()
        confirmed = request.get("overwrite_confirmed", False)
        if type(confirmed) is not bool:
            raise ValueError("overwrite_confirmed must be boolean")
        if (target.exists() or target.is_symlink()) and not confirmed:
            raise ValueError("Existing target requires explicit overwrite confirmation")
        started = time.monotonic()
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            write_data(
                value,
                str(target),
                format=request.get("format"),
                args=request.get("args"),
                kwargs=request.get("kwargs"),
            )
        return {
            "target": str(target),
            "object_id": payload["object_id"],
            "selector": payload.get("selector"),
            "format": request.get("format"),
            "duration_s": time.monotonic() - started,
            "warnings": [str(item.message) for item in caught],
            "details": {
                "datatype": type(value).__name__,
                "args": request.get("args", []),
                "kwargs": request.get("kwargs", {}),
            },
        }
