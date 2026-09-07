import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest
from gwexpy.timeseries import TimeSeries, TimeSeriesDict

from gwexpy_studio.domain.graph import OperationGraph
from gwexpy_studio.domain.model import (
    ActivityRecord,
    DataObjectRef,
    ExecutionRecord,
    Operation,
    PlotSpec,
)
from gwexpy_studio.domain.project import Project
from gwexpy_studio.export import export_python
from gwexpy_studio.ops.registry import REGISTRY


def pipeline_project(tmp_path: Path, *, container: bool = False):
    raw = TimeSeries(
        np.random.default_rng(17).normal(size=2048),
        sample_rate=128,
        t0=42,
        unit="m",
        name="A",
    )
    source = TimeSeriesDict(a=raw, b=raw * 2) if container else raw
    path = tmp_path / "input.h5"
    source.write(path, format="hdf5")
    operations = [
        Operation(
            op_id="op-read",
            operation_id="data.read",
            operation_schema=1,
            params={
                "datatype": type(source).__name__,
                "source": str(path),
                "format": "hdf5",
            },
            outputs=("obj-raw",),
        )
    ]
    values = {"obj-raw": source}
    objects = [
        DataObjectRef(
            object_id="obj-raw",
            kind=type(source).__name__,
            shape=raw.shape,
            dtype="float64",
            unit="m",
            produced_by="op-read",
        )
    ]
    executions = []
    steps = [
        ("timeseries.lowpass", "obj-filter", {"self": "obj-raw"}, {"frequency": 15.0}),
        (
            "timeseries.transfer_function",
            "obj-tf",
            {"self": "obj-raw", "other": "obj-filter"},
            {"fftlength": 2.0},
        ),
    ]
    if container:
        steps.append(
            (
                "data.extract",
                "obj-selected",
                {"self": "obj-tf"},
                {"selector": {"key": "b"}},
            )
        )
    for index, (name, output, inputs, params) in enumerate(steps):
        op_id = "op-" + str(index)
        result = REGISTRY[name].apply(
            {role: values[value] for role, value in inputs.items()}, params
        )
        details = {}
        if name == "timeseries.lowpass":
            details, result = result.details, result.value
        values[output] = result
        operations.append(
            Operation(
                op_id=op_id,
                operation_id=name,
                operation_schema=1,
                inputs=inputs,
                params=params,
                outputs=(output,),
            )
        )
        objects.append(
            DataObjectRef(
                object_id=output,
                kind=type(result).__name__,
                shape=(),
                dtype=None,
                unit=None,
                produced_by=op_id,
            )
        )
        executions.append(
            ExecutionRecord(
                execution_id="ex-" + str(index),
                op_id=op_id,
                started_at="2026-09-05",
                duration_s=0.0,
                status="succeeded",
                details=details,
            )
        )
    project = Project(
        objects=tuple(objects),
        graph=OperationGraph(operations),
        executions=tuple(executions),
    )
    return project, values


@pytest.mark.contract("SIG-0002")
def test_extended_export_replays_exact_coefficients_and_complex_native_values(tmp_path):
    project, expected = pipeline_project(tmp_path)
    operations = list(project.graph.operations)
    operations[1] = replace(operations[1], params={"frequency": 9999.0})
    project.graph = OperationGraph(operations)
    project.executions += (
        replace(
            project.executions[0],
            execution_id="failed-later",
            status="failed",
            details={"filter_recipes": []},
        ),
    )
    source = export_python(project, deterministic=True)
    assert "gwexpy_studio" not in source
    assert "science_filter_from_recipes" in source
    namespace = {"__name__": "__main__"}
    exec(source, namespace)
    actual = namespace["obj_tf"]
    np.testing.assert_allclose(actual.value, expected["obj-tf"].value)
    np.testing.assert_array_equal(actual.frequencies, expected["obj-tf"].frequencies)
    assert actual.unit == expected["obj-tf"].unit
    assert np.iscomplexobj(actual.value)


@pytest.mark.contract("SIG-0003")
def test_container_export_preserves_pairing_extraction_and_units(tmp_path):
    project, expected = pipeline_project(tmp_path, container=True)
    source = export_python(project, targets=("obj-selected",), deterministic=True)
    namespace = {"__name__": "__main__"}
    exec(source, namespace)
    np.testing.assert_allclose(
        namespace["obj_selected"].value, expected["obj-selected"].value
    )
    assert list(namespace["obj_tf"]) == ["a", "b"]
    assert namespace["obj_filter"]["a"].unit == expected["obj-filter"]["a"].unit


@pytest.mark.contract("SIG-0004")
def test_write_activities_are_explicit_opt_in_and_only_successes_replay(tmp_path):
    project, _ = pipeline_project(tmp_path)
    output = tmp_path / "written.h5"
    failed = tmp_path / "failed.h5"
    activity = ActivityRecord(
        activity_id="write-1",
        action="write_data",
        object_id="obj-filter",
        target=str(output),
        status="succeeded",
        started_at="2026-09-05",
        details={"request": {"format": "hdf5", "args": [], "kwargs": {}}},
    )
    project.activities = (
        activity,
        replace(activity, activity_id="write-2", status="failed", target=str(failed)),
    )
    source = export_python(project)
    assert str(output) not in source
    source = export_python(project, targets=("obj-tf",), include_data_writes=True)
    exec(source, {"__name__": "__main__"})
    assert output.exists()
    assert not failed.exists()


@pytest.mark.contract("SIG-0005")
def test_bode_and_complex_line_plots_match_display_oracle(tmp_path):
    from gwexpy_studio.plotting.renderer import render_plot

    project, expected = pipeline_project(tmp_path)
    project.plots = (
        PlotSpec(
            plot_id="bode",
            kind="bode",
            object_ids=("obj-tf",),
            phase_unwrap=True,
            magnitude_scale="db",
            db_reference=2.0,
            legend=True,
        ),
        PlotSpec(
            plot_id="imaginary",
            kind="line",
            object_ids=("obj-tf",),
            component="imag",
            title="imaginary",
        ),
    )
    source = export_python(project)
    namespace = {"__name__": "__main__"}
    exec(source, namespace)
    for spec in project.plots:
        figure = namespace["figures"][spec.plot_id]
        oracle = render_plot(spec, expected)
        for actual_ax, expected_ax in zip(figure.axes, oracle.axes, strict=True):
            for actual_line, expected_line in zip(
                actual_ax.lines, expected_ax.lines, strict=True
            ):
                np.testing.assert_allclose(
                    actual_line.get_xdata(), expected_line.get_xdata(), equal_nan=True
                )
                np.testing.assert_allclose(
                    actual_line.get_ydata(), expected_line.get_ydata(), equal_nan=True
                )
            assert actual_ax.get_ylabel() == expected_ax.get_ylabel()
    np.testing.assert_array_equal(namespace["obj_tf"].value, expected["obj-tf"].value)


@pytest.mark.contract("SIG-0006")
def test_extended_script_runs_in_fresh_process_without_project_imports(tmp_path):
    project, expected = pipeline_project(tmp_path)
    output = tmp_path / "transfer.npy"
    source = export_python(project, value_dumps={"obj-tf": output})
    script = tmp_path / "standalone.py"
    script.write_text(source, encoding="utf-8")
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    completed = subprocess.run(
        [sys.executable, str(script)],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    np.testing.assert_allclose(np.load(output), expected["obj-tf"].value)
    assert json.loads(output.with_suffix(".unit.json").read_text())["unit"] == str(
        expected["obj-tf"].unit
    )


@pytest.mark.contract("SIG-0066")
def test_member_frequency_dict_plot_replays_in_fresh_process(tmp_path):
    from gwexpy.frequencyseries import FrequencySeries, FrequencySeriesDict

    from gwexpy_studio.application.preview import PreviewData
    from gwexpy_studio.application.signal_preview import default_native_plot
    from gwexpy_studio.plotting.renderer import render_plot

    members = FrequencySeriesDict(
        a=FrequencySeries([1 + 2j, 3 + 4j, 5 + 6j], f0=1, df=1, unit="m"),
        b=FrequencySeries([7 + 8j, 9 + 10j, 11 + 12j], f0=1, df=1, unit="m"),
    )
    path = tmp_path / "members.h5"
    members.write(path, format="hdf5")
    project = Project(
        objects=(
            DataObjectRef(
                object_id="obj-tf",
                kind="FrequencySeriesDict",
                shape=(2,),
                dtype=None,
                unit=None,
                produced_by="op-read",
            ),
        ),
        graph=OperationGraph(
            [
                Operation(
                    op_id="op-read",
                    operation_id="data.read",
                    operation_schema=1,
                    params={
                        "datatype": "FrequencySeriesDict",
                        "source": str(path),
                        "format": "hdf5",
                    },
                    outputs=("obj-tf",),
                )
            ]
        ),
    )
    member = members["b"]
    preview = PreviewData(
        ref=DataObjectRef(
            object_id="obj-tf",
            kind="FrequencySeries",
            shape=member.shape,
            dtype=str(member.dtype),
            unit=str(member.unit),
            name=member.name,
            metadata={"member_selector": {"key": "b"}},
        ),
        values=member.value.copy(),
        unit=str(member.unit),
        x_coordinates=member.frequencies.to_value("Hz"),
    )
    saved = default_native_plot(preview)
    project.plots = (
        saved,
        replace(saved, plot_id="imaginary-member", kind="line", component="imag"),
    )
    assert saved.selectors == {"obj-tf": {"key": "b"}}
    source = export_python(project, targets=("obj-tf",))
    assert "gwexpy_studio" not in source
    output = tmp_path / "plot-lines.npz"
    script = tmp_path / "member-plot.py"
    script.write_text(
        source
        + "\nnp.savez("
        + repr(str(output))
        + ", **{f'{plot_id}_{i}_{j}_{axis}': getattr(line, f'get_{axis}data')() "
        "for plot_id, figure in figures.items() "
        "for i, ax in enumerate(figure.axes) for j, line in enumerate(ax.lines) "
        "for axis in ('x', 'y')})\n",
        encoding="utf-8",
    )
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    completed = subprocess.run(
        [sys.executable, str(script)],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    with np.load(output) as actual:
        for spec in project.plots:
            oracle = render_plot(replace(spec, selectors={}), {"obj-tf": member})
            for i, ax in enumerate(oracle.axes):
                for j, line in enumerate(ax.lines):
                    for axis in ("x", "y"):
                        np.testing.assert_allclose(
                            actual[f"{spec.plot_id}_{i}_{j}_{axis}"],
                            getattr(line, f"get_{axis}data")(),
                        )
    assert list(members) == ["a", "b"]


@pytest.mark.contract("SIG-0067")
def test_selector_plots_include_native_helpers_for_legacy_graph(tmp_path):
    project, _ = pipeline_project(tmp_path)
    operation = project.graph.operations[0]
    project.graph = OperationGraph(
        [
            replace(
                operation,
                operation_id="timeseries.read",
                params={"source": operation.params["source"], "format": "hdf5"},
            )
        ]
    )
    project.plots = (
        PlotSpec(
            plot_id="selected",
            kind="line",
            object_ids=("obj-raw",),
            selectors={"obj-raw": {"key": "a"}},
        ),
    )
    source = export_python(project, targets=("obj-raw",))
    assert "def native_extract(" in source
