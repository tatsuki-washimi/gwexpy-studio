"""Confirmed native reads and separately recorded external data writes."""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from ..domain.model import ActivityRecord, DataObjectRef, DataSourceRef
from ..errors import OperationError
from ..ops.intake import (
    inspection_sha256,
    normalize_io_request_paths,
    restore_inspection,
)
from ..worker.protocol import UUID4, RequestEnvelope, encode_message

if TYPE_CHECKING:
    from ..domain.project import Project
    from .signal_host import SignalHost


def _validation_payload(inspection: Mapping[str, Any]) -> dict[str, Any]:
    expected = inspection.get("inspection_sha256")
    current = inspection_sha256(inspection)
    if expected is None:
        expected = current
    elif expected != current:
        raise ValueError(
            "Read options changed after inspection; inspect and confirm again"
        )
    return {"request": dict(inspection["request"]), "expected_sha256": expected}


def _preflight(kind: Literal["inspect_io", "execute"], payload: dict[str, Any]) -> None:
    request: RequestEnvelope = {
        "protocol": 2,
        "type": kind,
        "request_id": UUID4(str(uuid.uuid4())),
        "payload": payload,
    }
    encode_message(request)


def _read_params(inspection: Mapping[str, Any], source: Any) -> dict[str, Any]:
    request = inspection["request"]
    return {
        "datatype": request["datatype"],
        "source": source,
        "format": request["format"],
        "args": request["args"],
        "kwargs": request["kwargs"],
        "gwexpy_version": inspection["gwexpy_version"],
    }


def _preflight_reads(project: Project, inspection: Mapping[str, Any]) -> None:
    """Check the session execute envelope before confirmation or graph changes."""
    request = inspection["request"]
    paths = request["paths"]
    combined = request["combine"] == "combined"
    source = (
        paths
        if combined
        else max(
            paths,
            key=lambda path: len(json.dumps(path, ensure_ascii=False).encode("utf-8")),
        )
    )
    # The longest path and final sequential IDs bound every individual request.
    offset = 0 if combined else len(paths) - 1
    op_id = f"op-{int(project.new_operation_id().removeprefix('op-')) + offset}"
    object_id = f"obj-{int(project.new_object_id().removeprefix('obj-')) + offset}"
    _preflight(
        "execute",
        {
            "operation": {
                "op_id": op_id,
                "operation_id": "data.read",
                "operation_schema": 1,
                "inputs": {},
                "params": _read_params(inspection, source),
                "outputs": [object_id],
            },
            "graph_id": project.project_id,
            "operation_id": op_id,
            "input_object_ids": [],
            "output_object_id": object_id,
        },
    )


class SignalIOController:
    """Delegate file formats to the worker's native GWexpy APIs."""

    def catalog_io(self: SignalHost, datatype: str, direction: str) -> dict[str, Any]:
        """Return registry candidates, allowing callers to enter other formats."""
        result = self._signal_request(
            "catalog_io", {"datatype": datatype, "direction": direction}
        )
        return dict(result["io_catalog"])

    def inspect_io(self: SignalHost, request: Mapping[str, Any]) -> dict[str, Any]:
        """Inspect native inputs before a read is confirmed."""
        normalized = normalize_io_request_paths(request)
        inspection = restore_inspection(
            self._signal_request(
                "inspect_io", {"request": normalized, "compact": True}
            ),
            normalized,
        )
        _preflight("inspect_io", _validation_payload(inspection))
        _preflight_reads(self.project, inspection)
        return inspection

    def read_io(
        self: SignalHost, inspection: Mapping[str, Any]
    ) -> tuple[DataObjectRef, ...]:
        """Read a previously reviewed input set with identity revalidation."""
        request = dict(inspection["request"])
        _preflight_reads(self.project, inspection)
        self._signal_request("inspect_io", _validation_payload(inspection))
        paths = request["paths"]
        sources = []
        for path in paths:
            root = next(
                (entry for entry in inspection["roots"] if entry["path"] == path), None
            )
            sources.append(
                DataSourceRef(
                    source_id=self.project.new_source_id(),
                    uri=path,
                    format=request["format"],
                    size_bytes=root["size_bytes"] if root is not None else None,
                    mtime=root["mtime_ns"] / 1e9 if root is not None else None,
                )
            )
            self.project.sources += (sources[-1],)
        grouped = (
            [(paths, tuple(item.source_id for item in sources))]
            if request["combine"] == "combined"
            else [
                (path, (item.source_id,))
                for path, item in zip(paths, sources, strict=True)
            ]
        )
        results = []
        for source, source_ids in grouped:
            obj = self._execute_signal(
                "data.read", {}, _read_params(inspection, source)
            )
            if obj.produced_by is not None:
                self.project.source_bindings = {
                    **self.project.source_bindings,
                    obj.produced_by: source_ids,
                }
            results.append(obj)
        return tuple(results)

    def write_data(
        self: SignalHost,
        object_id: str,
        request: Mapping[str, Any],
        selector: Mapping[str, Any] | None = None,
    ) -> ActivityRecord:
        """Write selected data, recording success or incomplete output separately."""
        self._object(object_id)
        paths = request.get("paths")
        if (
            not isinstance(paths, list)
            or len(paths) != 1
            or not isinstance(paths[0], str)
        ):
            raise OperationError("Choose one output path", code="invalid_params")
        target = Path(paths[0]).expanduser().absolute()
        if (target.exists() or target.is_symlink()) and request.get(
            "overwrite_confirmed"
        ) is not True:
            raise OperationError(
                "Overwrite confirmation is required",
                code="overwrite_confirmation_required",
            )
        handle = {"object_id": object_id, "selector": selector}
        resolved_id = self._materialize_handle(handle)
        normalized = {**request, "paths": [str(target)]}
        actual = self._object(resolved_id)
        if normalized.get("datatype", actual.kind) != actual.kind:
            raise OperationError(
                f"Selected data is {actual.kind}; choose that output data type",
                code="invalid_input_kind",
            )
        start = datetime.now(UTC).isoformat()
        t0 = time.monotonic()
        try:
            result = self._signal_request(
                "write_data", {"object_id": resolved_id, "request": normalized}
            )
        except Exception as exc:
            record = ActivityRecord(
                activity_id=str(uuid.uuid4()),
                action="write_data",
                object_id=resolved_id,
                target=str(target),
                started_at=start,
                status="failed",
                duration_s=time.monotonic() - t0,
                details={"request": normalized},
                error={
                    "code": getattr(exc, "code", "operation_failed"),
                    "message": str(exc),
                },
            )
            self.project.activities += (record,)
            raise OperationError(
                f"Data output incomplete: {exc}", code="data_write_failed"
            ) from exc
        record = ActivityRecord(
            activity_id=str(uuid.uuid4()),
            action="write_data",
            object_id=resolved_id,
            target=str(target),
            started_at=start,
            status="succeeded",
            duration_s=time.monotonic() - t0,
            details={"request": normalized, **result.get("details", {})},
            warnings=tuple(result.get("warnings", ())),
        )
        self.project.activities += (record,)
        return record
