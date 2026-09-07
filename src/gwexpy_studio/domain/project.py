"""Project aggregate-root interface."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from ..errors import OperationError, ProjectFormatError
from .graph import OperationGraph
from .history import HistoryState
from .model import (
    ActivityRecord,
    DataObjectRef,
    DataSourceRef,
    ExecutionRecord,
    Operation,
    PlotSpec,
)
from .project_fields import (
    _TOP_LEVEL_KEYS,
    _VALID_OBJECT_KINDS,
    _VALID_PLOT_KINDS,
    _VALID_SOURCE_FORMATS,
    _check_finite_recursive,
    _is_valid_unit_string,
    _require_bool,
    _require_dict,
    _require_dict_of_dict,
    _require_dict_of_str,
    _require_float,
    _require_int,
    _require_list,
    _require_optional_dict,
    _require_optional_float,
    _require_optional_int,
    _require_optional_limit,
    _require_optional_str,
    _require_str,
    _require_str_tuple,
)
from .project_v2 import (
    _validate_json,
    activity_from_dict,
    object_from_dict,
    plot_options_from_dict,
    record_dict,
)
from .project_v3 import history_from_dict, source_manifests_from_dict, validate_history
from .sources import source_bindings_from_dict, validate_source_bindings


@dataclass(kw_only=True, slots=True)
class Project:
    """Versioned project aggregate containing only data-oriented state."""

    schema_version: int = 3
    project_id: str = ""
    created: str = ""
    modified: str = ""
    compatibility: Mapping[str, str] = field(default_factory=dict)
    sources: tuple[DataSourceRef, ...] = ()
    objects: tuple[DataObjectRef, ...] = ()
    graph: OperationGraph = field(default_factory=OperationGraph)
    executions: tuple[ExecutionRecord, ...] = ()
    plots: tuple[PlotSpec, ...] = ()
    ui_state: Mapping[str, Any] = field(default_factory=dict)
    activities: tuple[ActivityRecord, ...] = ()

    history: HistoryState = field(default_factory=HistoryState)
    source_manifests: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    source_bindings: Mapping[str, tuple[str, ...]] = field(default_factory=dict)

    def validate(self) -> None:
        """Validate reference integrity across all aggregate members."""
        op_by_id: dict[str, Any] = {op.op_id: op for op in self.graph.operations}
        materialized_object_ids: set[str] = {obj.object_id for obj in self.objects}
        activity_ids = [activity.activity_id for activity in self.activities]
        if len(activity_ids) != len(set(activity_ids)):
            raise ProjectFormatError("Duplicate activity_id in activities")

        validate_history(self)
        validate_source_bindings(self)
        source_manifests_from_dict(self.source_manifests)
        if not set(self.source_manifests) <= op_by_id.keys():
            raise ProjectFormatError("source_manifests: unknown operation ID")

        # 1. Validate objects
        for obj in self.objects:
            if obj.produced_by is not None:
                if obj.produced_by not in op_by_id:
                    raise ProjectFormatError(
                        f"Object '{obj.object_id}' refers to missing producer "
                        f"op_id '{obj.produced_by}'"
                    )
                producer = op_by_id[obj.produced_by]
                if obj.object_id not in producer.outputs:
                    raise ProjectFormatError(
                        f"Object '{obj.object_id}' produced_by '{obj.produced_by}' "
                        f"which does not declare it in outputs: {producer.outputs}"
                    )
                for role, in_id in producer.inputs.items():
                    if in_id not in materialized_object_ids:
                        raise ProjectFormatError(
                            f"Materialized object '{obj.object_id}' requires producer "
                            f"'{producer.op_id}' input '{in_id}' to be materialized"
                        )

        # 2. Validate operations inputs: must refer to declared outputs
        # or materialized objects
        all_declared_output_ids = {
            out_id for op in self.graph.operations for out_id in op.outputs
        } | materialized_object_ids
        for op in self.graph.operations:
            for role, input_id in op.inputs.items():
                if input_id not in all_declared_output_ids:
                    raise ProjectFormatError(
                        f"Operation '{op.op_id}' input '{role}'='{input_id}' "
                        "is not declared as an output or materialized in "
                        "project objects"
                    )

        # 3. Validate executions
        for exec_record in self.executions:
            if exec_record.op_id not in op_by_id:
                raise ProjectFormatError(
                    f"Execution '{exec_record.execution_id}' refers to unknown "
                    f"op_id '{exec_record.op_id}'"
                )

        # 4. Validate plots
        for plot in self.plots:
            for obj_id in plot.object_ids:
                if obj_id not in all_declared_output_ids:
                    raise ProjectFormatError(
                        f"Plot '{plot.plot_id}' refers to unknown object_id '{obj_id}'"
                    )

    def new_source_id(self) -> str:
        """Allocate the next sequential source ID."""
        max_idx = 0
        for s in self.sources:
            match = re.fullmatch(r"src-(\d+)", s.source_id)
            if match:
                max_idx = max(max_idx, int(match.group(1)))
        return f"src-{max_idx + 1}"

    def new_object_id(self) -> str:
        """Allocate the next sequential object ID."""
        max_idx = 0
        for obj in self.objects:
            match = re.fullmatch(r"obj-(\d+)", obj.object_id)
            if match:
                max_idx = max(max_idx, int(match.group(1)))
        for op in self.graph.operations:
            for output_id in op.outputs:
                match = re.fullmatch(r"obj-(\d+)", output_id)
                if match:
                    max_idx = max(max_idx, int(match.group(1)))
        return f"obj-{max_idx + 1}"

    def new_operation_id(self) -> str:
        """Allocate the next sequential operation ID."""
        max_idx = 0
        for op in self.graph.operations:
            match = re.fullmatch(r"op-(\d+)", op.op_id)
            if match:
                max_idx = max(max_idx, int(match.group(1)))
        return f"op-{max_idx + 1}"

    def new_activity_id(self) -> str:
        """Allocate the next sequential external activity ID."""
        indices = [
            int(m.group(1))
            for activity in self.activities
            if (m := re.fullmatch(r"activity-(\d+)", activity.activity_id))
        ]
        return f"activity-{max(indices, default=0) + 1}"

    def to_dict(self) -> dict[str, Any]:
        """Serialize the project into a JSON-primitive dictionary."""
        return {
            "schema_version": self.schema_version,
            "project_id": self.project_id,
            "created": self.created,
            "modified": self.modified,
            "compatibility": dict(self.compatibility),
            "sources": [
                {
                    "source_id": s.source_id,
                    "uri": s.uri,
                    "format": s.format,
                    "size_bytes": s.size_bytes,
                    "mtime": s.mtime,
                }
                for s in self.sources
            ],
            "objects": [
                {
                    "object_id": o.object_id,
                    "kind": o.kind,
                    "shape": list(o.shape),
                    "dtype": o.dtype,
                    "unit": o.unit,
                    "name": o.name,
                    "channel": o.channel,
                    "axes": {k: dict(v) for k, v in o.axes.items()},
                    "produced_by": o.produced_by,
                    **(
                        {
                            "metadata": record_dict(o.metadata),
                            "members": [record_dict(member) for member in o.members],
                        }
                        if self.schema_version >= 2
                        else {}
                    ),
                }
                for o in self.objects
            ],
            "operations": [
                {
                    "op_id": op.op_id,
                    "operation_id": op.operation_id,
                    "operation_schema": op.operation_schema,
                    "inputs": dict(op.inputs),
                    "params": dict(op.params),
                    "outputs": list(op.outputs),
                }
                for op in self.graph.operations
            ],
            "executions": [
                {
                    "execution_id": ex.execution_id,
                    "op_id": ex.op_id,
                    "started_at": ex.started_at,
                    "duration_s": ex.duration_s,
                    "status": ex.status,
                    "warnings": list(ex.warnings),
                    "error": dict(ex.error) if ex.error is not None else None,
                    "environment": dict(ex.environment),
                    **(
                        {"details": record_dict(ex.details)}
                        if self.schema_version >= 2
                        else {}
                    ),
                }
                for ex in self.executions
            ],
            "plots": [
                {
                    "plot_id": p.plot_id,
                    "kind": p.kind,
                    "object_ids": list(p.object_ids),
                    "xscale": p.xscale,
                    "yscale": p.yscale,
                    "xlim": list(p.xlim) if p.xlim is not None else None,
                    "ylim": list(p.ylim) if p.ylim is not None else None,
                    **(
                        {
                            "phase_ylim": list(p.phase_ylim)
                            if p.phase_ylim is not None
                            else None
                        }
                        if self.schema_version >= 3
                        else {}
                    ),
                    "title": p.title,
                    "xlabel": p.xlabel,
                    "ylabel": p.ylabel,
                    "legend": p.legend,
                    "styles": dict(p.styles),
                    **(
                        {
                            "component": p.component,
                            "magnitude_scale": p.magnitude_scale,
                            "db_reference": p.db_reference,
                            "display_unit": p.display_unit,
                            "phase_unwrap": p.phase_unwrap,
                            "selectors": record_dict(p.selectors),
                        }
                        if self.schema_version >= 2
                        else {}
                    ),
                }
                for p in self.plots
            ],
            "ui_state": dict(self.ui_state),
            **(
                {
                    "history": record_dict(self.history),
                    "source_manifests": record_dict(self.source_manifests),
                    "source_bindings": record_dict(self.source_bindings),
                }
                if self.schema_version >= 3
                else {}
            ),
            **(
                {"activities": [record_dict(a) for a in self.activities]}
                if self.schema_version >= 2
                else {}
            ),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Project:
        """Load and strictly validate a project from a mapping."""
        if not isinstance(value, Mapping):
            raise ProjectFormatError("Project document must be a mapping")

        # Check for unknown top-level keys
        unknown_keys = set(value.keys()) - (
            _TOP_LEVEL_KEYS
            | ({"activities"} if value.get("schema_version") in (2, 3) else set())
            | (
                {"history", "source_manifests", "source_bindings"}
                if value.get("schema_version") == 3
                else set()
            )
        )
        if unknown_keys:
            raise ProjectFormatError(f"Unknown top-level keys: {sorted(unknown_keys)}")

        schema_version = value.get("schema_version")
        if type(schema_version) is not int or schema_version not in (1, 2, 3):
            raise ProjectFormatError(
                f"Unsupported schema version: {schema_version!r} (expected 1, 2, or 3)"
            )

        _check_finite_recursive(value)
        if schema_version >= 3:
            _validate_json(value, "project")
        else:
            for index, plot in enumerate(_require_list(value, "plots", "")):
                if isinstance(plot, Mapping) and "phase_ylim" in plot:
                    raise ProjectFormatError(
                        f"plots[{index}].phase_ylim: v3 fields are not valid "
                        "in older versions"
                    )
        if schema_version == 1:
            for section, extensions in (
                ("objects", {"metadata", "members"}),
                ("executions", {"details"}),
                (
                    "plots",
                    {
                        "component",
                        "magnitude_scale",
                        "db_reference",
                        "display_unit",
                        "phase_unwrap",
                        "selectors",
                    },
                ),
            ):
                for index, record in enumerate(_require_list(value, section, "")):
                    if isinstance(record, Mapping) and set(record) & extensions:
                        raise ProjectFormatError(
                            f"{section}[{index}]: v2 fields are not valid in v1"
                        )

        project_id = _require_str(value, "project_id", "")
        created = _require_str(value, "created", "")
        modified = _require_str(value, "modified", "")

        # compatibility: preserve the full mapping (round-trip fidelity for
        # any additional keys a future minor schema revision might add) but
        # require its two documented keys to be present and str-typed —
        # closing the "missing key silently becomes {}" / untyped-passthrough
        # gap without introducing a new additionalProperties rejection that
        # no existing contract exercises.
        compatibility_raw = _require_dict(value, "compatibility", "")
        _require_str(compatibility_raw, "studio", "compatibility")
        _require_str(compatibility_raw, "gwexpy", "compatibility")
        compatibility = dict(compatibility_raw)

        ui_state = dict(_require_dict(value, "ui_state", ""))

        # Parse sources
        raw_sources = _require_list(value, "sources", "")
        sources_list: list[DataSourceRef] = []
        for index, s in enumerate(raw_sources):
            item_path = f"sources[{index}]"
            if not isinstance(s, Mapping):
                raise ProjectFormatError(f"{item_path}: source item must be a mapping")
            fmt = _require_str(s, "format", item_path)
            valid_format = isinstance(fmt, str) and (
                bool(fmt.strip()) and len(fmt) <= 128
                if schema_version >= 2
                else fmt in _VALID_SOURCE_FORMATS
            )
            if not valid_format:
                raise ProjectFormatError(
                    f"{item_path}.format: invalid source format: {fmt!r}"
                )
            sources_list.append(
                DataSourceRef(
                    source_id=_require_str(s, "source_id", item_path),
                    uri=_require_str(s, "uri", item_path),
                    format=fmt,
                    size_bytes=_require_optional_int(s, "size_bytes", item_path),
                    mtime=_require_optional_float(s, "mtime", item_path),
                )
            )

        # Parse objects
        raw_objects = _require_list(value, "objects", "")
        objects_list: list[DataObjectRef] = []
        for index, o in enumerate(raw_objects):
            item_path = f"objects[{index}]"
            if not isinstance(o, Mapping):
                raise ProjectFormatError(f"{item_path}: object item must be a mapping")
            if schema_version >= 2:
                objects_list.append(object_from_dict(o, item_path))
                continue
            kind = o.get("kind")
            if kind not in _VALID_OBJECT_KINDS:
                raise ProjectFormatError(
                    f"{item_path}.kind: invalid object kind: {kind!r}"
                )
            unit_str = o.get("unit")
            if not isinstance(unit_str, str) or not _is_valid_unit_string(unit_str):
                raise ProjectFormatError(
                    f"{item_path}.unit: invalid unit string: {unit_str!r}"
                )
            raw_shape = _require_list(o, "shape", item_path)
            for dim_index, dim in enumerate(raw_shape):
                if type(dim) is bool or not isinstance(dim, int) or dim < 0:
                    raise ProjectFormatError(
                        f"{item_path}.shape[{dim_index}]: invalid dimension: {dim!r}"
                    )
            axes_raw = _require_dict_of_dict(o, "axes", item_path)

            objects_list.append(
                DataObjectRef(
                    object_id=_require_str(o, "object_id", item_path),
                    kind=kind,
                    shape=tuple(raw_shape),
                    dtype=_require_str(o, "dtype", item_path),
                    unit=unit_str,
                    name=_require_optional_str(o, "name", item_path),
                    channel=_require_optional_str(o, "channel", item_path),
                    axes={k: dict(v) for k, v in axes_raw.items()},
                    produced_by=_require_optional_str(o, "produced_by", item_path),
                )
            )

        # Parse operations
        raw_ops = _require_list(value, "operations", "")
        operations_list: list[Operation] = []
        for index, op_data in enumerate(raw_ops):
            item_path = f"operations[{index}]"
            if not isinstance(op_data, Mapping):
                raise ProjectFormatError(
                    f"{item_path}: operation item must be a mapping"
                )
            outputs = _require_str_tuple(op_data, "outputs", item_path)

            operations_list.append(
                Operation(
                    op_id=_require_str(op_data, "op_id", item_path),
                    operation_id=_require_str(op_data, "operation_id", item_path),
                    operation_schema=_require_int(
                        op_data, "operation_schema", item_path
                    ),
                    inputs=dict(_require_dict_of_str(op_data, "inputs", item_path)),
                    params=dict(_require_dict(op_data, "params", item_path)),
                    outputs=outputs,
                )
            )

        # An operation input may legitimately name an object that is never
        # produced by any operation in this document (e.g. materialized
        # directly from a source, with no synthetic "read" operation in the
        # graph). Such ids must be exempt from the forward-reference check
        # below, but ONLY when no operation anywhere in the document (not
        # just earlier ones) claims to produce them — otherwise an object
        # that genuinely is produced later would let a cycle slip through
        # simply by also being listed in "objects". Excluding the full
        # output union (not just already-seen outputs) is what keeps this
        # exemption from reopening the forward-reference/cycle hole.
        all_operation_output_ids = {
            out_id for op in operations_list for out_id in op.outputs
        }
        known_root_object_ids = {
            obj.object_id for obj in objects_list
        } - all_operation_output_ids

        # OperationGraph.add() is the single validation entry point for DAG
        # invariants (duplicate op_id, duplicate output, forward reference /
        # unknown input) — see graph.py. It is exercised here in bulk via
        # __init__, so a project document that injects a cycle, a forward
        # reference, or a duplicate op_id/output is rejected the same way an
        # incrementally built graph would reject it. The former standalone
        # duplicate-output pre-check in this loop was removed as redundant;
        # confirmed via grep that no test asserts on its exact message text
        # (only on ProjectFormatError being raised), so folding the check
        # into the graph does not break an existing contract.
        try:
            graph = OperationGraph(
                operations_list, known_root_object_ids=known_root_object_ids
            )
        except OperationError as exc:
            raise ProjectFormatError(str(exc)) from exc

        # Parse executions
        raw_execs = _require_list(value, "executions", "")
        op_id_set = {op.op_id for op in operations_list}
        executions_list: list[ExecutionRecord] = []
        for index, ex in enumerate(raw_execs):
            item_path = f"executions[{index}]"
            if not isinstance(ex, Mapping):
                raise ProjectFormatError(
                    f"{item_path}: execution item must be a mapping"
                )
            op_id = _require_str(ex, "op_id", item_path)
            if op_id not in op_id_set:
                raise ProjectFormatError(
                    f"{item_path}.op_id: references unknown operation: {op_id}"
                )
            error_raw = _require_optional_dict(ex, "error", item_path)
            executions_list.append(
                ExecutionRecord(
                    execution_id=_require_str(ex, "execution_id", item_path),
                    op_id=op_id,
                    started_at=_require_str(ex, "started_at", item_path),
                    duration_s=_require_float(ex, "duration_s", item_path),
                    status=_require_str(ex, "status", item_path),
                    warnings=_require_str_tuple(ex, "warnings", item_path),
                    error=dict(error_raw) if error_raw is not None else None,
                    environment=dict(
                        _require_dict_of_str(ex, "environment", item_path)
                    ),
                    details=dict(_require_dict(ex, "details", item_path))
                    if schema_version >= 2 and "details" in ex
                    else {},
                )
            )

        # Parse plots
        raw_plots = _require_list(value, "plots", "")
        plots_list: list[PlotSpec] = []
        for index, p in enumerate(raw_plots):
            item_path = f"plots[{index}]"
            if not isinstance(p, Mapping):
                raise ProjectFormatError(f"{item_path}: plot item must be a mapping")
            kind = p.get("kind")
            if kind not in (
                _VALID_PLOT_KINDS | ({"bode"} if schema_version >= 2 else set())
            ):
                raise ProjectFormatError(
                    f"{item_path}.kind: invalid plot kind: {kind!r}"
                )
            plots_list.append(
                PlotSpec(
                    plot_id=_require_str(p, "plot_id", item_path),
                    kind=kind,
                    object_ids=_require_str_tuple(p, "object_ids", item_path),
                    xscale=_require_str(p, "xscale", item_path),
                    yscale=_require_str(p, "yscale", item_path),
                    xlim=_require_optional_limit(p, "xlim", item_path),
                    ylim=_require_optional_limit(p, "ylim", item_path),
                    phase_ylim=_require_optional_limit(p, "phase_ylim", item_path)
                    if schema_version >= 3 and "phase_ylim" in p
                    else None,
                    title=_require_optional_str(p, "title", item_path),
                    xlabel=_require_optional_str(p, "xlabel", item_path),
                    ylabel=_require_optional_str(p, "ylabel", item_path),
                    legend=_require_bool(p, "legend", item_path),
                    styles=dict(_require_dict(p, "styles", item_path)),
                    **(
                        plot_options_from_dict(p, item_path)
                        if schema_version >= 2
                        else {}
                    ),
                )
            )

        project = cls(
            schema_version=3,
            project_id=project_id,
            created=created,
            modified=modified,
            compatibility=compatibility,
            sources=tuple(sources_list),
            objects=tuple(objects_list),
            graph=graph,
            executions=tuple(executions_list),
            plots=tuple(plots_list),
            ui_state=ui_state,
            history=history_from_dict(_require_dict(value, "history", ""))
            if schema_version == 3
            else HistoryState(),
            source_manifests=source_manifests_from_dict(
                _require_dict(value, "source_manifests", "")
            )
            if schema_version == 3
            else {},
            source_bindings=source_bindings_from_dict(value.get("source_bindings", {}))
            if schema_version == 3
            else {},
            activities=tuple(
                activity_from_dict(a, f"activities[{i}]")
                for i, a in enumerate(_require_list(value, "activities", ""))
            )
            if schema_version >= 2 and "activities" in value
            else (),
        )
        project.validate()
        return project
