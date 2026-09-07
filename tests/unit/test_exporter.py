"""Contract tests for deterministic OperationGraph-to-Python export."""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from collections.abc import Collection
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import numpy as np
import pytest
from astropy import units as u

from gwexpy_studio.domain.model import (
    DataObjectRef,
    DataSourceRef,
    ExecutionRecord,
    Operation,
)
from gwexpy_studio.domain.project import Project
from gwexpy_studio.export.python_exporter import export_python
from tests.support.fixtures import CHANNEL_NAME, TEST_NAME
from tests.support.g0_consistency import assert_csv_worker_and_export


class _GraphFixture:
    """Deterministic graph fixture that isolates exporter contracts from graph tests."""

    def __init__(self, operations: Collection[Operation]) -> None:
        self._operations = tuple(operations)

    @property
    def operations(self) -> tuple[Operation, ...]:
        return self._operations

    def producer_of(self, object_id: str) -> Operation | None:
        return next(
            (
                operation
                for operation in self._operations
                if object_id in operation.outputs
            ),
            None,
        )

    def ancestors(self, target_object_ids: Collection[str]) -> tuple[Operation, ...]:
        wanted = set(target_object_ids)
        selected: set[str] = set()
        changed = True
        while changed:
            changed = False
            for operation in self._operations:
                if operation.op_id in selected:
                    continue
                if any(output in wanted for output in operation.outputs):
                    selected.add(operation.op_id)
                    wanted.update(operation.inputs.values())
                    changed = True
        return tuple(
            operation for operation in self._operations if operation.op_id in selected
        )


def _base_project(hdf5_source: Path) -> Project:
    """Build a branch-shaped project without invoking the domain graph skeleton."""
    source = DataSourceRef(
        source_id="src-1",
        uri=str(hdf5_source.resolve()),
        format="hdf5",
        size_bytes=hdf5_source.stat().st_size,
        mtime=hdf5_source.stat().st_mtime,
    )
    raw = DataObjectRef(
        object_id="obj-1",
        kind="TimeSeries",
        shape=(15360,),
        dtype="float64",
        unit="m",
        name=TEST_NAME,
        channel=CHANNEL_NAME,
        axes={"t0": {"value": 1_000_000_000.0, "unit": "s"}},
    )
    cropped = replace(raw, object_id="obj-2", produced_by="op-2")
    asd = DataObjectRef(
        object_id="obj-3",
        kind="FrequencySeries",
        shape=(513,),
        dtype="float64",
        unit="m / Hz(1/2)",
        produced_by="op-3",
        axes={"f0": {"value": 0.0, "unit": "Hz"}, "df": {"value": 0.25, "unit": "Hz"}},
    )
    spectrogram = DataObjectRef(
        object_id="obj-4",
        kind="Spectrogram",
        shape=(15, 257),
        dtype="float64",
        unit="m2 / Hz",
        produced_by="op-4",
        axes={
            "t0": {"value": 1_000_000_000.0, "unit": "s"},
            "dt": {"value": 4.0, "unit": "s"},
            "f0": {"value": 0.0, "unit": "Hz"},
            "df": {"value": 0.5, "unit": "Hz"},
        },
    )
    operations = (
        Operation(
            op_id="op-1",
            operation_id="timeseries.read",
            operation_schema=1,
            params={
                "source": {"source_id": "src-1"},
                "format": "hdf5",
                "name": TEST_NAME,
            },
            outputs=("obj-1",),
        ),
        Operation(
            op_id="op-2",
            operation_id="timeseries.crop",
            operation_schema=1,
            inputs={"self": "obj-1"},
            params={"start": 1_000_000_001.0, "end": 1_000_000_010.0},
            outputs=("obj-2",),
        ),
        Operation(
            op_id="op-3",
            operation_id="timeseries.asd",
            operation_schema=1,
            inputs={"self": "obj-2"},
            params={"fftlength": {"value": 4000.0, "unit": "ms"}},
            outputs=("obj-3",),
        ),
        Operation(
            op_id="op-4",
            operation_id="timeseries.spectrogram",
            operation_schema=1,
            inputs={"self": "obj-1"},
            params={
                "stride": {"value": 4.0, "unit": "s"},
                "fftlength": {"value": 2.0, "unit": "s"},
            },
            outputs=("obj-4",),
        ),
        Operation(
            op_id="op-5",
            operation_id="timeseries.asd",
            operation_schema=1,
            inputs={"self": "obj-2"},
            params={"fftlength": {"value": 8.0, "unit": "s"}},
            outputs=("obj-5",),
        ),
    )
    graph = _GraphFixture(operations)
    return Project(
        schema_version=1,
        project_id="project-1",
        created="2026-08-16T00:00:00Z",
        modified="2026-08-16T00:00:00Z",
        sources=(source,),
        objects=(raw, cropped, asd, spectrogram),
        graph=cast(Any, graph),
        executions=(
            ExecutionRecord(
                execution_id="exec-1",
                op_id="op-5",
                started_at="2026-08-16T00:00:00Z",
                duration_s=0.1,
                status="failed",
                error={"code": "operation_failed", "message": "failed output"},
            ),
        ),
    )


def _scientific_calls(source: str) -> list[ast.Call]:
    """Return generated scientific method calls in source order."""
    tree = ast.parse(source)

    class Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.calls: list[ast.Call] = []

        def visit_Call(self, node: ast.Call) -> None:
            if isinstance(node.func, ast.Attribute) and node.func.attr in {
                "read",
                "crop",
                "detrend",
                "asd",
                "spectrogram",
            }:
                self.calls.append(node)
            self.generic_visit(node)

    visitor = Visitor()
    visitor.visit(tree)
    return visitor.calls


def _literal_keyword(call: ast.Call, name: str) -> Any:
    """Evaluate one keyword value from a generated call AST."""
    keyword = next((item for item in call.keywords if item.arg == name), None)
    assert keyword is not None, f"missing keyword {name!r}"
    return ast.literal_eval(keyword.value)


def _call_name(call: ast.Call) -> str:
    """Return a filtered scientific call's method name."""
    assert isinstance(call.func, ast.Attribute)
    return call.func.attr


def _literal_values(node: ast.AST) -> list[Any]:
    """Evaluate all literal constants nested in one generated call."""
    return [item.value for item in ast.walk(node) if isinstance(item, ast.Constant)]


@pytest.mark.contract("B-036")
def test_export_is_deterministic_ast_parseable_and_studio_free(
    hdf5_source: Path,
) -> None:
    """Deterministic export is a standalone Python artifact."""
    project = _base_project(hdf5_source)

    first = export_python(project, deterministic=True)
    second = export_python(project, deterministic=True)

    assert first == second
    ast.parse(first)
    assert "gwexpy_studio" not in first
    assert "from gwexpy.timeseries import TimeSeries" in first


@pytest.mark.contract("B-037")
def test_export_explicit_target_contains_only_its_branch_closure(
    hdf5_source: Path,
) -> None:
    """An explicit ASD target exports read/crop/ASD, not the sibling branch."""
    source = export_python(
        _base_project(hdf5_source), targets=("obj-3",), deterministic=True
    )

    calls = _scientific_calls(source)

    assert [_call_name(call) for call in calls] == ["read", "crop", "asd"]
    assert len(calls) == 3
    assert len({_call_name(call) for call in calls}) == 3
    assert _literal_keyword(calls[-1], "fftlength") == 4.0


@pytest.mark.contract("B-038")
def test_export_default_materialized_leaf_excludes_failed_output(
    hdf5_source: Path,
) -> None:
    """Default targets use materialized objects and omit failed declared outputs."""
    source = export_python(_base_project(hdf5_source), deterministic=True)

    tree = ast.parse(source)
    calls = _scientific_calls(source)
    assert [_call_name(call) for call in calls] == [
        "read",
        "crop",
        "asd",
        "spectrogram",
    ]
    assert len(calls) == 4
    assert len({_call_name(call) for call in calls}) == 4
    asd_calls = [call for call in calls if _call_name(call) == "asd"]
    assert len(asd_calls) == 1
    assert _literal_keyword(asd_calls[0], "fftlength") == 4.0
    constants = [
        item.value for item in ast.walk(tree) if isinstance(item, ast.Constant)
    ]
    assert "obj-5" not in constants


@pytest.mark.contract("B-039")
def test_export_escapes_source_literals_and_keeps_generated_ast_valid(
    hdf5_source: Path,
) -> None:
    """Paths and names are emitted as escaped Python literals."""
    project = _base_project(hdf5_source)
    weird_uri = "/tmp/O'Reilly\nwave.h5"
    source_ref = replace(project.sources[0], uri=weird_uri)
    first_operation = project.graph.operations[0]
    graph = _GraphFixture(
        (
            replace(
                first_operation,
                params={
                    **first_operation.params,
                    "name": "X1:'quoted'\nchannel",
                },
            ),
            *project.graph.operations[1:],
        )
    )
    project = replace(project, sources=(source_ref,), graph=cast(Any, graph))

    source = export_python(project, targets=("obj-1",), deterministic=True)

    read_calls = [
        call for call in _scientific_calls(source) if _call_name(call) == "read"
    ]
    assert len(read_calls) == 1
    literals = _literal_values(read_calls[0])
    assert weird_uri in literals
    assert "X1:'quoted'\nchannel" in literals


@pytest.mark.contract("B-040")
def test_export_omits_optional_defaults_and_emits_canonical_float(
    hdf5_source: Path,
) -> None:
    """Optional operation parameters stay omitted and saved units become seconds."""
    project = _base_project(hdf5_source)

    source = export_python(project, targets=("obj-3",), deterministic=True)

    calls = _scientific_calls(source)
    asd_calls = [call for call in calls if _call_name(call) == "asd"]
    assert len(asd_calls) == 1
    asd_call = asd_calls[0]
    assert _literal_keyword(asd_call, "fftlength") == 4.0
    keyword_names = {keyword.arg for keyword in asd_call.keywords if keyword.arg}
    assert keyword_names == {"fftlength"}


@pytest.mark.contract("B-041")
def test_export_value_dumps_emit_values_and_unit_artifacts(
    hdf5_source: Path, tmp_path: Path
) -> None:
    """Value dumps preserve array values and unit metadata at export boundary."""
    output = tmp_path / "asd.npy"

    source = export_python(
        _base_project(hdf5_source),
        targets=("obj-3",),
        value_dumps={"obj-3": output},
        deterministic=True,
    )

    tree = ast.parse(source)
    save_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "np"
        and node.func.attr == "save"
    ]
    assert len(save_calls) == 1
    assert str(output) in _literal_values(save_calls[0])
    metadata_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"dump", "dumps", "write_text"}
    ]
    assert metadata_calls
    assert any("unit" in _literal_values(node) for node in metadata_calls)


@pytest.mark.integration
@pytest.mark.contract("B-042")
def test_exported_script_executes_in_a_fresh_process(
    hdf5_source: Path, tmp_path: Path
) -> None:
    """The generated artifact executes and dumps values plus a unit sidecar."""
    output = tmp_path / "asd.npy"
    script = tmp_path / "generated.py"
    source = export_python(
        _base_project(hdf5_source),
        targets=("obj-3",),
        value_dumps={"obj-3": output},
        deterministic=True,
    )
    tree = ast.parse(source)
    imported_modules = [
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    ]
    imported_modules.extend(
        node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    )
    assert not any(module.startswith("gwexpy_studio") for module in imported_modules)
    script.write_text(source, encoding="utf-8")

    environment = os.environ.copy()
    completed = subprocess.run(
        [sys.executable, str(script)],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert output.is_file()
    unit_sidecar = output.with_suffix(".unit.json")
    assert unit_sidecar.is_file()

    dumped = np.load(output, allow_pickle=False)
    unit_payload = json.loads(unit_sidecar.read_text(encoding="utf-8"))
    assert set(unit_payload) == {"unit"}

    from gwexpy.timeseries import TimeSeries

    reference = TimeSeries.read(hdf5_source, format="hdf5", name=TEST_NAME)
    reference = reference.crop(1_000_000_001.0, 1_000_000_010.0).asd(fftlength=4.0)
    dumped_values = np.asarray(dumped)
    reference_values = np.asarray(reference.value)
    assert np.isfinite(dumped_values).all()
    assert np.isfinite(reference_values).all()
    dumped_unit = u.Unit(unit_payload["unit"])
    reference_unit = u.Unit(reference.unit)
    assert dumped_unit == reference_unit
    dumped_in_reference_unit = (dumped_values * dumped_unit).to_value(reference_unit)
    tolerance = 1e-12 * max(1.0, float(np.max(np.abs(reference_values))))
    np.testing.assert_allclose(
        dumped_in_reference_unit,
        reference_values,
        rtol=1e-12,
        atol=tolerance,
    )
    assert_csv_worker_and_export(tmp_path)
