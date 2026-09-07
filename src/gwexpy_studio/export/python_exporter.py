"""OperationGraph-to-Python exporter implementation."""

from __future__ import annotations

import pathlib
from collections.abc import Collection, Mapping
from dataclasses import asdict
from typing import Any

from ..domain.history import active_object_ids, active_targets, object_closure
from ..domain.model import ActivityRecord
from ..domain.project import Project
from ..ops.registry import REGISTRY
from ..ops.spec import normalize_params
from ..ops.timeseries import REGISTRY as LEGACY_REGISTRY
from .active_writes import active_writes
from .standalone_sources import native_helper_source, plotting_helper_source


def _emit_crop_validation(
    receiver: str, params: Mapping[str, Any], suffix: str
) -> list[str]:
    """Emit the Studio crop span checks using only public GWexpy attributes."""
    start = params.get("start")
    end = params.get("end")
    if start is None and end is None:
        return []

    t0_var = f"_crop_t0_{suffix}"
    end_var = f"_crop_end_{suffix}"
    lines = [
        f"{t0_var} = float({receiver}.t0.to_value('s'))",
        f"{end_var} = {t0_var} + float({receiver}.duration.to_value('s'))",
    ]
    if start is not None:
        start_literal = repr(float(start))
        lines.extend(
            [
                f"if {start_literal} < {t0_var} or {start_literal} > {end_var}:",
                f"    raise ValueError('Crop start {start_literal} is outside "
                f"series span [' + str({t0_var}) + ', ' + str({end_var}) + ']')",
            ]
        )
    if end is not None:
        end_literal = repr(float(end))
        lines.extend(
            [
                f"if {end_literal} < {t0_var} or {end_literal} > {end_var}:",
                f"    raise ValueError('Crop end {end_literal} is outside "
                f"series span [' + str({t0_var}) + ', ' + str({end_var}) + ']')",
            ]
        )
    if start is not None and end is not None:
        lines.extend(
            [
                f"if {start_literal} >= {end_literal}:",
                f"    raise ValueError('Crop start ({start_literal}) must be "
                f"strictly less than end ({end_literal})')",
            ]
        )
    return lines


def export_python(
    project: Project,
    *,
    targets: Collection[str] | None = None,
    value_dumps: Mapping[str, pathlib.Path] | None = None,
    deterministic: bool = False,
    include_data_writes: bool = False,
) -> str:
    """Export a project graph slice into a standalone Python script."""
    del deterministic
    wanted_targets = active_targets(project) if targets is None else tuple(targets)
    writes: tuple[ActivityRecord, ...] = ()
    if include_data_writes:
        write_scope = active_object_ids(project)
        if targets is not None:
            write_scope &= object_closure(project, wanted_targets)
        writes = active_writes(project, write_scope)
    wanted_targets = tuple(
        dict.fromkeys(
            (
                *wanted_targets,
                *(
                    record.object_id
                    for record in writes
                    if record.object_id is not None
                ),
            )
        )
    )
    ancestors = project.graph.ancestors(wanted_targets)
    ancestor_ids = {op.op_id for op in ancestors}

    ordered_ops = [op for op in project.graph.operations if op.op_id in ancestor_ids]

    sources_map = {s.source_id: s.uri for s in project.sources}
    kinds = {obj.object_id: obj.kind for obj in project.objects}
    selected = {output for op in ordered_ops for output in op.outputs}
    selected_plots = [
        plot
        for plot in project.plots
        if plot.object_ids and set(plot.object_ids) <= selected
    ]
    specs = {}
    for op in ordered_ops:
        spec = REGISTRY.get(op.operation_id)
        if (
            op.operation_id in LEGACY_REGISTRY
            and op.operation_id != "timeseries.read"
            and kinds.get(op.inputs.get("self", "")) == "TimeSeries"
        ):
            spec = LEGACY_REGISTRY[op.operation_id]
        specs[op.op_id] = spec
    needs_helpers = (
        bool(writes)
        or any(plot.selectors for plot in selected_plots)
        or any(
            spec is not None and spec is not LEGACY_REGISTRY.get(op.operation_id)
            for op in ordered_ops
            if (spec := specs[op.op_id]) is not None
        )
    )
    details_by_op = {
        execution.op_id: execution.details
        for execution in project.executions
        if execution.status in ("succeeded", "success")
    }

    lines: list[str] = [
        '"""Generated standalone script exported from GWexpy Studio."""',
        "",
        "from __future__ import annotations",
        "",
    ]

    if value_dumps:
        lines.extend(
            [
                "import json",
                "from pathlib import Path",
                "",
                "import numpy as np",
                "",
            ]
        )

    lines.extend(
        [
            "import gwexpy",
            "",
            "gwexpy.register_all(include_io=False)",
            "",
            "from gwexpy.timeseries import TimeSeries",
            "",
        ]
    )

    if needs_helpers:
        lines.extend(
            [
                "# Reuse the same GWexpy version and native I/O plugins.",
                native_helper_source(),
                "",
            ]
        )
    if selected_plots:
        lines.extend([plotting_helper_source(), ""])

    for op in ordered_ops:
        spec = specs[op.op_id]
        if spec is None:
            continue

        target_var = op.outputs[0].replace("-", "_") if op.outputs else "out"
        inputs_map = {k: v.replace("-", "_") for k, v in op.inputs.items()}

        raw_params = dict(op.params)
        if "source" in raw_params and isinstance(raw_params["source"], dict):
            src_id = raw_params["source"].get("source_id", "")
            raw_params["source"] = sources_map.get(src_id, src_id)

        norm_params = normalize_params(raw_params, spec=spec)
        context = {
            "variable": target_var,
            "target": target_var,
            "source": inputs_map.get("self", "ts"),
            "inputs": inputs_map,
            "params": norm_params,
            "details": details_by_op.get(op.op_id, {}),
        }
        if (
            op.operation_id == "timeseries.crop"
            and spec is LEGACY_REGISTRY[op.operation_id]
        ):
            lines.extend(
                _emit_crop_validation(
                    inputs_map["self"],
                    norm_params,
                    op.op_id.replace("-", "_"),
                )
            )
        line = spec.emit(context)
        lines.append(f"# {op.operation_id}")
        lines.append(line)

    if selected_plots:
        lines.append("figures = {}")
        for plot in selected_plots:
            objects = (
                "{"
                + ", ".join(
                    f"{object_id!r}: {object_id.replace('-', '_')}"
                    for object_id in plot.object_ids
                )
                + "}"
            )
            lines.append(
                f"figures[{plot.plot_id!r}] = render_plot("
                f"SimpleNamespace(**{asdict(plot)!r}), {objects})"
            )

    for record in writes:
        request = record.details.get("request", record.details)
        lines.append(f"# Recorded data export: {record.activity_id}")
        lines.append(
            f"write_data({str(record.object_id).replace('-', '_')}, {record.target!r}, "
            f"format={request.get('format')!r}, args={request.get('args', [])!r}, "
            f"kwargs={request.get('kwargs', {})!r})"
        )

    if value_dumps:
        lines.append("")
        for obj_id in sorted(value_dumps.keys()):
            dump_path = str(value_dumps[obj_id])
            var_name = obj_id.replace("-", "_")
            lines.append(f"np.save({repr(dump_path)}, {var_name}.value)")
            lines.append(
                f"Path({repr(dump_path)}).with_suffix('.unit.json').write_text("
                f"json.dumps({{'unit': str({var_name}.unit)}}), encoding='utf-8')"
            )

    lines.append("")
    return "\n".join(lines)
