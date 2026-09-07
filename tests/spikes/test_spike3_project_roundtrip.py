"""Spike 3 contracts for data-only projects and atomic persistence."""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from astropy import units as u

from gwexpy_studio.domain.model import (
    DataObjectRef,
    DataSourceRef,
    ExecutionRecord,
    Operation,
    PlotSpec,
)
from gwexpy_studio.domain.project import Project
from gwexpy_studio.errors import PrototypeNotImplementedError
from gwexpy_studio.ops.source import SourceInspection
from gwexpy_studio.persistence.project_io import load_project, save_project
from gwexpy_studio.session import StudioSession
from gwexpy_studio.worker.client import WorkerClient, WorkerLifecycle
from gwexpy_studio.worker.shm import attach_block
from tests.support.fixtures import make_timeseries, random_values, write_named_hdf5
from tests.support.sentinel import invoke_preserving_sentinel

pytestmark = pytest.mark.integration


class _GraphFixture:
    """Small graph implementation used only to construct a complete project."""

    def __init__(self, operations: tuple[Operation, ...]) -> None:
        self._operations = operations

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

    def ancestors(self, target_object_ids: tuple[str, ...]) -> tuple[Operation, ...]:
        selected: set[str] = set()
        pending = list(target_object_ids)
        while pending:
            object_id = pending.pop()
            operation = self.producer_of(object_id)
            if operation is None or operation.op_id in selected:
                continue
            selected.add(operation.op_id)
            pending.extend(operation.inputs.values())
        return tuple(
            operation for operation in self._operations if operation.op_id in selected
        )


def _complete_project(source: Path) -> tuple[Project, dict[str, Any]]:
    """Build all seven manifest sections and a failed operation without output."""
    source_ref = DataSourceRef(
        source_id="src-001",
        uri=str(source),
        format="hdf5",
        size_bytes=source.stat().st_size,
        mtime=123.0,
    )
    raw = DataObjectRef(
        object_id="obj-1",
        kind="TimeSeries",
        shape=(15360,),
        dtype="float64",
        unit="m",
        name="X1:STUDIO-TEST",
        channel="X1:STUDIO-CHANNEL",
        axes={
            "t0": {"value": 1_000_000_000.0, "unit": "s"},
            "dt": {"value": 1.0 / 256.0, "unit": "s"},
        },
        produced_by=None,
    )
    cropped = DataObjectRef(
        object_id="obj-2",
        kind="TimeSeries",
        shape=(2304,),
        dtype="float64",
        unit="m",
        name="X1:STUDIO-TEST-cropped",
        channel="X1:STUDIO-CHANNEL",
        axes={
            "t0": {"value": 1_000_000_001.0, "unit": "s"},
            "dt": {"value": 1.0 / 256.0, "unit": "s"},
        },
        produced_by="op-2",
    )
    detrended = DataObjectRef(
        object_id="obj-3",
        kind="TimeSeries",
        shape=(2304,),
        dtype="float64",
        unit="m",
        name="X1:STUDIO-TEST-detrended",
        channel="X1:STUDIO-CHANNEL",
        axes={
            "t0": {"value": 1_000_000_001.0, "unit": "s"},
            "dt": {"value": 1.0 / 256.0, "unit": "s"},
        },
        produced_by="op-3",
    )
    asd = DataObjectRef(
        object_id="obj-4",
        kind="FrequencySeries",
        shape=(513,),
        dtype="float64",
        unit="m / Hz(1/2)",
        name="X1:STUDIO-TEST-asd",
        channel="X1:STUDIO-CHANNEL",
        axes={"f0": {"value": 0.0, "unit": "Hz"}, "df": {"value": 0.25, "unit": "Hz"}},
        produced_by="op-4",
    )
    spectrogram = DataObjectRef(
        object_id="obj-5",
        kind="Spectrogram",
        shape=(5, 513),
        dtype="float64",
        unit="m2 / Hz",
        name="X1:STUDIO-TEST-spectrogram",
        channel="X1:STUDIO-CHANNEL",
        axes={
            "t0": {"value": 1_000_000_001.0, "unit": "s"},
            "dt": {"value": 4.0, "unit": "s"},
            "f0": {"value": 0.0, "unit": "Hz"},
            "df": {"value": 0.25, "unit": "Hz"},
        },
        produced_by="op-5",
    )
    operations = (
        Operation(
            op_id="op-1",
            operation_id="timeseries.read",
            operation_schema=1,
            params={"source": str(source), "format": "hdf5", "name": "X1:STUDIO-TEST"},
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
            operation_id="timeseries.detrend",
            operation_schema=1,
            inputs={"self": "obj-2"},
            params={"detrend": "linear"},
            outputs=("obj-3",),
        ),
        Operation(
            op_id="op-4",
            operation_id="timeseries.asd",
            operation_schema=1,
            inputs={"self": "obj-3"},
            params={"fftlength": {"value": 4.0, "unit": "s"}},
            outputs=("obj-4",),
        ),
        Operation(
            op_id="op-5",
            operation_id="timeseries.spectrogram",
            operation_schema=1,
            inputs={"self": "obj-3"},
            params={
                "stride": {"value": 4.0, "unit": "s"},
                "fftlength": {"value": 2.0, "unit": "s"},
            },
            outputs=("obj-5",),
        ),
        Operation(
            op_id="op-6",
            operation_id="timeseries.unknown",
            operation_schema=1,
            inputs={"self": "obj-3"},
            params={},
            outputs=("obj-6",),
        ),
    )
    executions = (
        ExecutionRecord(
            execution_id="exec-1",
            op_id="op-4",
            started_at="2026-08-16T00:00:00Z",
            duration_s=1.0,
            status="succeeded",
            environment={"python": "3.12.12", "gwexpy": "0.1.14"},
        ),
        ExecutionRecord(
            execution_id="exec-2",
            op_id="op-6",
            started_at="2026-08-16T00:00:01Z",
            duration_s=0.1,
            status="failed",
            error={"code": "operation_failed", "message": "unknown operation"},
            environment={"python": "3.12.12", "gwexpy": "0.1.14"},
        ),
    )
    plots = (
        PlotSpec(
            plot_id="plot-1",
            kind="line",
            object_ids=("obj-3",),
            xscale="linear",
            yscale="linear",
            xlim=(1_000_000_001.0, 1_000_000_010.0),
            ylim=None,
            title="detrended",
            xlabel="GPS seconds",
            ylabel="m",
            legend=True,
            styles={"color": "black"},
        ),
        PlotSpec(
            plot_id="plot-2",
            kind="spectrogram",
            object_ids=("obj-5",),
            xscale="linear",
            yscale="log",
            xlim=None,
            ylim=(0.25, 128.0),
            title="spectrogram",
            xlabel="Time [s]",
            ylabel="Frequency [Hz]",
            legend=False,
            styles={"cmap": "viridis"},
        ),
    )
    graph = _GraphFixture(operations)
    project = Project(
        schema_version=1,
        project_id="project-spike-3",
        created="2026-08-16T00:00:00Z",
        modified="2026-08-16T00:00:00Z",
        compatibility={"studio": "0.1.0", "gwexpy": "0.1.14"},
        sources=(source_ref,),
        objects=(raw, cropped, detrended, asd, spectrogram),
        graph=graph,
        executions=executions,
        plots=plots,
        ui_state={"selected_object": "obj-4", "layout": {"left": 320}},
    )
    manifest = {
        "schema_version": 1,
        "project_id": project.project_id,
        "created": project.created,
        "modified": project.modified,
        "compatibility": dict(project.compatibility),
        "sources": [
            {
                "source_id": source_ref.source_id,
                "uri": source_ref.uri,
                "format": source_ref.format,
                "size_bytes": source_ref.size_bytes,
                "mtime": source_ref.mtime,
            }
        ],
        "objects": [
            {
                "object_id": value.object_id,
                "kind": value.kind,
                "shape": list(value.shape),
                "dtype": value.dtype,
                "unit": value.unit,
                "name": value.name,
                "channel": value.channel,
                "axes": dict(value.axes),
                "produced_by": value.produced_by,
            }
            for value in project.objects
        ],
        "operations": [
            {
                "op_id": operation.op_id,
                "operation_id": operation.operation_id,
                "operation_schema": operation.operation_schema,
                "inputs": dict(operation.inputs),
                "params": dict(operation.params),
                "outputs": list(operation.outputs),
            }
            for operation in operations
        ],
        "executions": [
            {
                "execution_id": execution.execution_id,
                "op_id": execution.op_id,
                "started_at": execution.started_at,
                "duration_s": execution.duration_s,
                "status": execution.status,
                "warnings": list(execution.warnings),
                "error": None if execution.error is None else dict(execution.error),
                "environment": dict(execution.environment),
            }
            for execution in executions
        ],
        "plots": [
            {
                "plot_id": plot.plot_id,
                "kind": plot.kind,
                "object_ids": list(plot.object_ids),
                "xscale": plot.xscale,
                "yscale": plot.yscale,
                "xlim": None if plot.xlim is None else list(plot.xlim),
                "ylim": None if plot.ylim is None else list(plot.ylim),
                "title": plot.title,
                "xlabel": plot.xlabel,
                "ylabel": plot.ylabel,
                "legend": plot.legend,
                "styles": dict(plot.styles),
            }
            for plot in plots
        ],
        "ui_state": copy.deepcopy(dict(project.ui_state)),
    }
    return project, manifest


def _write_manifest(path: Path, manifest: Mapping[str, Any]) -> None:
    """Write deterministic JSON bytes for load-boundary tests."""
    path.write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True), encoding="utf-8"
    )


def _fresh_load_canary(
    *,
    project_path: Path,
    source: Path,
    adjacent_paths: tuple[Path, ...],
    expect_future_schema: bool = False,
) -> dict[str, Any]:
    """Audit load in a fresh interpreter with every approved execution seam sealed."""
    repository_root = Path(__file__).resolve().parents[2]
    probe = r"""
import builtins
import importlib
import importlib.util
import io
import json
import multiprocessing
import os
import pathlib
import runpy
import subprocess
import sys
from pathlib import Path

from gwexpy_studio.errors import ProjectFormatError, PrototypeNotImplementedError

project_path = Path(sys.argv[1])
forbidden_paths = {
    os.path.abspath(os.path.normpath(os.fspath(value)))
    for value in sys.argv[2:-1]
}
expect_future = sys.argv[-1] == "future"

forbidden_module_roots = (
    "gwexpy",
    "gwpy",
    "numpy",
    "scipy",
    "astropy",
    "h5py",
    "matplotlib",
    "gwexpy_studio.worker",
    "gwexpy_studio.runtime",
    "gwexpy_studio.ops",
    "gwexpy_studio.export",
    "gwexpy_studio.plotting",
    "gwexpy_studio.session",
)

def is_forbidden_module(name):
    return any(
        name == root or name.startswith(root + ".")
        for root in forbidden_module_roots
    )

before_project_io = {
    name for name in sys.modules if is_forbidden_module(name)
}

def is_forbidden(value):
    try:
        normalized = os.path.abspath(os.path.normpath(os.fspath(value)))
        return normalized in forbidden_paths
    except (TypeError, ValueError):
        return False

def reject(message):
    raise AssertionError(message)

original_open = builtins.open
original_io_open = io.open
original_file_io = io.FileIO
original_open_code = io.open_code
original_import = builtins.__import__
original_eval = builtins.eval
original_compile = builtins.compile
original_exec = builtins.exec
original_path = pathlib.Path
original_os_open = os.open
original_os_stat = os.stat
original_os_lstat = os.lstat
original_getsize = os.path.getsize
original_import_module = importlib.import_module
original_find_spec = importlib.util.find_spec
original_module_from_spec = importlib.util.module_from_spec
original_spec_from_file = importlib.util.spec_from_file_location
original_popen = subprocess.Popen
original_run_path = runpy.run_path
original_run_module = runpy.run_module
original_subprocess_run = subprocess.run
original_subprocess_call = subprocess.call
original_check_call = subprocess.check_call
original_check_output = subprocess.check_output
original_process = multiprocessing.Process
original_get_context = multiprocessing.get_context
original_os_process = {
    name: getattr(os, name)
    for name in dir(os)
    if (
        name.startswith(("spawn", "exec", "posix_spawn"))
        or name in {"system", "popen", "fork", "forkpty"}
    )
    and callable(getattr(os, name, None))
}

from gwexpy_studio.persistence.project_io import load_project

after_project_io = {
    name for name in sys.modules if is_forbidden_module(name)
}
assert not after_project_io - before_project_io, (
    "project_io preloaded forbidden modules: "
    + repr(sorted(after_project_io - before_project_io))
)

def guarded_open(value, *args, **kwargs):
    if is_forbidden(value):
        reject("load opened source or adjacent script")
    return original_open(value, *args, **kwargs)

def guarded_io_open(value, *args, **kwargs):
    if is_forbidden(value):
        reject("load used io.open on source or adjacent script")
    return original_io_open(value, *args, **kwargs)

def guarded_file_io(value, *args, **kwargs):
    if is_forbidden(value):
        reject("load used io.FileIO on source or adjacent script")
    return original_file_io(value, *args, **kwargs)

def guarded_open_code(value, *args, **kwargs):
    if is_forbidden(value):
        reject("load used io.open_code on source or adjacent script")
    return original_open_code(value, *args, **kwargs)

def guarded_path_method(method, label):
    def wrapper(self, *args, **kwargs):
        if is_forbidden(self):
            reject("load used pathlib." + label + " on source/script")
        return method(self, *args, **kwargs)
    return wrapper

def guarded_os_path(value, original, *args, **kwargs):
    if is_forbidden(value):
        reject("load used os path inspection on source/script")
    return original(value, *args, **kwargs)

def guarded_os_open(value, *args, **kwargs):
    if is_forbidden(value):
        reject("load used os.open on source/script")
    return original_os_open(value, *args, **kwargs)

def guarded_os_stat(value, *args, **kwargs):
    return guarded_os_path(value, original_os_stat, *args, **kwargs)

def guarded_os_lstat(value, *args, **kwargs):
    return guarded_os_path(value, original_os_lstat, *args, **kwargs)

def guarded_getsize(value):
    if is_forbidden(value):
        reject("load measured source/script size")
    return original_getsize(value)

def guarded_eval(*args, **kwargs):
    reject("load evaluated Python")

def guarded_compile(*args, **kwargs):
    reject("load compiled Python")

def guarded_import(name, *args, **kwargs):
    if is_forbidden_module(name):
        reject("load imported forbidden module: " + name)
    return original_import(name, *args, **kwargs)

def guarded_import_module(name, *args, **kwargs):
    if is_forbidden_module(name):
        reject("load used importlib.import_module: " + name)
    return original_import_module(name, *args, **kwargs)

def guarded_find_spec(name, *args, **kwargs):
    if is_forbidden_module(name):
        reject("load used importlib.util.find_spec: " + name)
    return original_find_spec(name, *args, **kwargs)

def guarded_module_from_spec(*args, **kwargs):
    reject("load constructed an importlib module")

def guarded_spec_from_file(*args, **kwargs):
    reject("load loaded a module from a file")

def guarded_exec(*args, **kwargs):
    reject("load executed Python")

def guarded_script(*args, **kwargs):
    reject("load executed runpy or subprocess")

def guarded_process(*args, **kwargs):
    reject("load created a process")

def audit_guard(event, args):
    if event in {"open", "os.open", "io.open_code"} and any(
        is_forbidden(value) for value in args
    ):
        reject("audit hook observed native source/script access")
    if event in {
        "os.fork",
        "os.forkpty",
        "os.posix_spawn",
        "os.posix_spawnp",
        "os.system",
        "subprocess.Popen",
    }:
        reject("audit hook observed process creation")

sys.addaudithook(audit_guard)

for name in (
    "stat",
    "lstat",
    "open",
    "read_bytes",
    "read_text",
    "resolve",
    "exists",
    "is_file",
    "is_dir",
):
    if hasattr(pathlib.Path, name):
        setattr(
            pathlib.Path,
            name,
            guarded_path_method(getattr(pathlib.Path, name), name),
        )
builtins.open = guarded_open
io.open = guarded_io_open
io.FileIO = guarded_file_io
io.open_code = guarded_open_code
builtins.__import__ = guarded_import
builtins.eval = guarded_eval
builtins.compile = guarded_compile
builtins.exec = guarded_exec
os.stat = guarded_os_stat
os.lstat = guarded_os_lstat
os.open = guarded_os_open
os.path.getsize = guarded_getsize
importlib.import_module = guarded_import_module
importlib.util.find_spec = guarded_find_spec
importlib.util.module_from_spec = guarded_module_from_spec
importlib.util.spec_from_file_location = guarded_spec_from_file
runpy.run_path = guarded_script
runpy.run_module = guarded_script
subprocess.run = guarded_script
subprocess.Popen = guarded_script
subprocess.call = guarded_script
subprocess.check_call = guarded_script
subprocess.check_output = guarded_script
multiprocessing.Process = guarded_process
multiprocessing.get_context = guarded_process
for name in original_os_process:
    setattr(os, name, guarded_script)
for module_name in (
    "multiprocessing", "multiprocessing.spawn", "multiprocessing.forkserver"
):
    module = sys.modules.get(module_name)
    if module is not None:
        for name in ("Process", "get_context", "spawn", "fork"):
            if hasattr(module, name):
                setattr(module, name, guarded_process)

for module in list(sys.modules.values()):
    if module is None:
        continue
    if module is sys.modules.get(__name__):
        continue
    aliases = {
        original_open: guarded_open,
        original_io_open: guarded_io_open,
        original_file_io: guarded_file_io,
        original_open_code: guarded_open_code,
        original_import: guarded_import,
        original_eval: guarded_eval,
        original_compile: guarded_compile,
        original_exec: guarded_exec,
        original_os_open: guarded_os_open,
        original_os_stat: guarded_os_stat,
        original_os_lstat: guarded_os_lstat,
        original_getsize: guarded_getsize,
        original_popen: guarded_script,
        original_run_path: guarded_script,
        original_run_module: guarded_script,
        original_subprocess_run: guarded_script,
        original_subprocess_call: guarded_script,
        original_check_call: guarded_script,
        original_check_output: guarded_script,
        original_process: guarded_process,
        original_get_context: guarded_process,
        original_import_module: guarded_import_module,
        original_find_spec: guarded_find_spec,
        original_module_from_spec: guarded_module_from_spec,
        original_spec_from_file: guarded_spec_from_file,
    }
    aliases.update({value: guarded_script for value in original_os_process.values()})
    try:
        module_items = list(vars(module).items())
    except Exception:
        continue
    for name, value in module_items:
        try:
            replacement = aliases.get(value)
        except TypeError:
            continue
        if replacement is not None:
            setattr(module, name, replacement)

forbidden_after_project_io = {
    name for name in sys.modules if is_forbidden_module(name)
}
new_forbidden = sorted(forbidden_after_project_io - before_project_io)
assert not new_forbidden, new_forbidden
scientific_roots = (
    "gwexpy",
    "gwpy",
    "numpy",
    "scipy",
    "astropy",
    "h5py",
    "matplotlib",
)
assert not any(
    any(name == root or name.startswith(root + ".") for root in scientific_roots)
    for name in forbidden_after_project_io
)

# Reading the project manifest itself is the only file read permitted here.
with builtins.open(project_path, "rb") as manifest_stream:
    manifest = json.load(manifest_stream)
assert manifest["project_id"]
with io.open(project_path, "r", encoding="utf-8") as manifest_stream:
    assert json.load(manifest_stream)["project_id"] == manifest["project_id"]

try:
    loaded = load_project(project_path)
except PrototypeNotImplementedError as error:
    print(json.dumps({"status": "sentinel", "code": error.code, "owner": error.owner}))
except ProjectFormatError as error:
    if not expect_future:
        raise
    print(json.dumps({"status": "future_rejected", "message": str(error)}))
else:
    if expect_future:
        raise AssertionError("future project schema was accepted")
    print(json.dumps({
        "status": "ok",
        "project_id": loaded.project_id,
        "source_mtime": loaded.sources[0].mtime,
        "object_ids": [value.object_id for value in loaded.objects],
    }))
"""
    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.pathsep.join(
        [
            str(repository_root / "src"),
            str(repository_root),
            environment.get("PYTHONPATH", ""),
        ]
    )
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            probe,
            str(project_path),
            str(source),
            *(str(path) for path in adjacent_paths),
            "future" if expect_future_schema else "normal",
        ],
        cwd=repository_root / "tests",
        env=environment,
        capture_output=True,
        text=False,
        check=False,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr.decode("utf-8", errors="replace")
    return json.loads(completed.stdout.decode("utf-8").splitlines()[-1])


def _raise_reported_sentinel(report: Mapping[str, Any], owner: str) -> None:
    """Re-raise a fresh-child sentinel so the contract keeps its real owner."""
    if report.get("status") != "sentinel":
        return
    assert report["owner"] == owner
    raise PrototypeNotImplementedError(
        code=str(report["code"]), owner=str(report["owner"])
    )


def _load_canary_or_raise(
    *,
    project_path: Path,
    source: Path,
    adjacent_paths: tuple[Path, ...],
    expect_future_schema: bool = False,
) -> dict[str, Any]:
    """Run the fresh canary while preserving the first public sentinel."""
    report = _fresh_load_canary(
        project_path=project_path,
        source=source,
        adjacent_paths=adjacent_paths,
        expect_future_schema=expect_future_schema,
    )
    _raise_reported_sentinel(
        report, "gwexpy_studio.persistence.project_io.load_project"
    )
    return report


def _inspect_with_mtime_warning(session: StudioSession) -> SourceInspection:
    """Constrain one warning call while preserving the current sentinel."""
    inspection: SourceInspection
    try:
        with pytest.warns(UserWarning, match="source mtime changed") as recorded:
            inspection = session.inspect_source("src-001")
    except BaseException as error:
        candidates = (error, error.__context__)
        sentinel = next(
            (
                candidate
                for candidate in candidates
                if isinstance(candidate, PrototypeNotImplementedError)
                and type(candidate) is PrototypeNotImplementedError
                and candidate.code == "STUDIO-FOUNDATION-NOT-IMPLEMENTED"
                and candidate.owner
                == "gwexpy_studio.session.StudioSession.inspect_source"
            ),
            None,
        )
        if sentinel is not None:
            raise sentinel
        raise
    assert len(recorded) == 1
    return inspection


@pytest.mark.contract("I-S3-001")
def test_complete_project_roundtrip_is_atomic_and_clocked(
    tmp_path: Path, request: pytest.FixtureRequest
) -> None:
    """All project sections round-trip with injected time and no temporary artifact."""
    source = tmp_path / "source.h5"
    write_named_hdf5(make_timeseries(random_values()), source)
    project, manifest = _complete_project(source)
    # Captured before any replay so the ADR-0006 non-destructive guarantee
    # below can be checked as byte-for-byte (reference) identity, not just
    # id-set equality: a partial replay of the ASD branch must not touch
    # the untouched spectrogram branch's obj-5.
    original_objects = tuple(project.objects)
    path = tmp_path / "project.gwxproj"

    invoke_preserving_sentinel(
        lambda: save_project(
            project,
            path,
            clock=lambda: "2026-08-16T01:02:03Z",
        ),
        owner="gwexpy_studio.persistence.project_io.save_project",
    )
    assert path.is_file()
    assert project.modified == "2026-08-16T01:02:03Z"
    assert {item.name for item in tmp_path.iterdir()} == {
        "source.h5",
        "project.gwxproj",
    }
    document = json.loads(path.read_text(encoding="utf-8"))
    assert set(document) == set(manifest)
    assert document["modified"] == "2026-08-16T01:02:03Z"
    loaded = load_project(path)
    from dataclasses import replace

    assert loaded.schema_version == 3
    assert replace(loaded, schema_version=1).to_dict() == document
    assert loaded.graph.operations == project.graph.operations
    assert u.Unit(loaded.objects[3].unit) == u.Unit("m / Hz(1/2)")
    assert loaded.plots[1].styles == {"cmap": "viridis"}
    assert loaded.executions[1].status == "failed"
    assert not any(value.object_id == "obj-6" for value in loaded.objects)
    before_ids = tuple(value.object_id for value in project.objects)
    before_metadata = tuple(project.objects)
    assert tuple(value.object_id for value in loaded.objects) == before_ids
    assert tuple(loaded.objects) == before_metadata

    # Cache-less recovery is a second, explicit worker/session round-trip.  The
    # first save call above remains the exact current persistence sentinel.
    cache = tmp_path / ".gwxcache"
    cache.mkdir()
    stale_cache = cache / "stale.npy"
    stale_cache.write_bytes(b"must not be authoritative")
    stale_cache_snapshot = (stale_cache.read_bytes(), stale_cache.stat().st_mtime_ns)
    client = WorkerClient()
    session = StudioSession(project=project, client=client)
    worker_sessions: list[tuple[WorkerClient, StudioSession]] = [(client, session)]

    def close_worker_sessions() -> None:
        """Close every started cache-less worker even after a deep assertion fails."""
        for worker_client, worker_session in reversed(worker_sessions):
            if worker_client.state is WorkerLifecycle.RUNNING:
                worker_session.close()
                assert worker_client.state is WorkerLifecycle.CLOSED

    request.addfinalizer(close_worker_sessions)
    invoke_preserving_sentinel(
        session.start,
        owner="gwexpy_studio.worker.client.WorkerClient.start",
    )
    session.replay(targets=("obj-4",))
    # ADR-0006: a partial replay of the ASD branch never touches the
    # untouched spectrogram branch (obj-5) -- Project.objects is unchanged.
    assert tuple(project.objects) == original_objects
    pre_save_result = client.get_array("obj-4", preview_stride=None)
    pre_save_objects_response = client.request(
        {
            "protocol": 2,
            "request_id": "00000000-0000-4000-8000-000000000803",
            "type": "list_objects",
            "payload": {},
        }
    )
    assert pre_save_objects_response["type"] == "result"
    pre_save_object_ids = tuple(pre_save_objects_response["payload"]["object_ids"])
    # Only the replayed ASD branch's ancestor closure (op-1..op-4) is sent to
    # the worker.  The untouched spectrogram branch (obj-5) is neither
    # executed nor deleted -- ADR-0006 keeps it in Project.objects even
    # though it was never resident in the worker's object store.
    assert set(pre_save_object_ids) == {"obj-1", "obj-2", "obj-3", "obj-4"}
    assert set(pre_save_object_ids) <= {value.object_id for value in project.objects}
    assert tuple(value.object_id for value in project.objects) == (
        "obj-1",
        "obj-2",
        "obj-3",
        "obj-4",
        "obj-5",
    )
    pre_save_refs = tuple(project.objects)
    pre_save_descriptor = pre_save_result["descriptor"]
    pre_save_attachment = attach_block(
        pre_save_descriptor,
        preview_stride=None,
    )
    pre_save_values = pre_save_attachment.values.copy()
    pre_save_attachment.handle.close()
    client.request(
        {
            "protocol": 2,
            "request_id": "00000000-0000-4000-8000-000000000801",
            "type": "release_shm",
            "payload": {"name": pre_save_descriptor.name},
        }
    )

    second_path = tmp_path / "cacheless.gwxproj"
    save_project(project, second_path, clock=lambda: "2026-08-16T01:03:04Z")
    loaded_for_replay = load_project(second_path)
    loaded_client = WorkerClient()
    loaded_session = StudioSession(project=loaded_for_replay, client=loaded_client)
    worker_sessions.append((loaded_client, loaded_session))
    invoke_preserving_sentinel(
        loaded_session.start,
        owner="gwexpy_studio.worker.client.WorkerClient.start",
    )
    loaded_session.replay(targets=("obj-4",))
    loaded_result = loaded_client.get_array("obj-4", preview_stride=None)
    loaded_descriptor = loaded_result["descriptor"]
    loaded_attachment = attach_block(
        loaded_descriptor,
        preview_stride=None,
    )
    try:
        assert loaded_result["unit"] == pre_save_result["unit"]
        assert loaded_descriptor.shape == pre_save_descriptor.shape
        assert loaded_descriptor.dtype == pre_save_descriptor.dtype
        assert loaded_attachment.values.shape == pre_save_values.shape
        assert np.array_equal(loaded_attachment.values, pre_save_values)
    finally:
        loaded_attachment.handle.close()
        loaded_client.request(
            {
                "protocol": 2,
                "request_id": "00000000-0000-4000-8000-000000000802",
                "type": "release_shm",
                "payload": {"name": loaded_descriptor.name},
            }
        )
    assert tuple(value.object_id for value in loaded_for_replay.objects) == tuple(
        value.object_id for value in project.objects
    )
    assert tuple(loaded_for_replay.objects) == tuple(project.objects)
    loaded_objects_response = loaded_client.request(
        {
            "protocol": 2,
            "request_id": "00000000-0000-4000-8000-000000000804",
            "type": "list_objects",
            "payload": {},
        }
    )
    assert loaded_objects_response["type"] == "result"
    assert tuple(loaded_objects_response["payload"]["object_ids"]) == (
        pre_save_object_ids
    )
    assert tuple(loaded_for_replay.objects) == pre_save_refs
    assert stale_cache.read_bytes() == stale_cache_snapshot[0]
    assert stale_cache.stat().st_mtime_ns == stale_cache_snapshot[1]


@pytest.mark.contract("I-S3-002")
def test_project_load_is_strictly_data_only_and_preserves_source_snapshot(
    tmp_path: Path,
) -> None:
    """Load never stats/reads source data or executes adjacent code."""
    source = tmp_path / "source.h5"
    write_named_hdf5(make_timeseries(random_values()), source)
    adjacent = tmp_path / "adjacent.py"
    config = tmp_path / "config.py"
    project_stem = tmp_path / "project-spike-3.py"
    marker = tmp_path / "executed.txt"
    adjacent.write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('executed')\n",
        encoding="utf-8",
    )
    config.write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('config')\n",
        encoding="utf-8",
    )
    project_stem.write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('stem')\n",
        encoding="utf-8",
    )
    _project, manifest = _complete_project(source)
    manifest["sources"][0]["mtime"] = 123.0
    path = tmp_path / "project.gwxproj"
    _write_manifest(path, manifest)
    os.utime(source, (999.0, 999.0))
    source_snapshot = (source.read_bytes(), source.stat().st_mtime_ns)
    changed_inspection = SourceInspection(
        exists=True,
        size_bytes=source.stat().st_size,
        mtime=999.0,
        format_guess="hdf5",
    )
    inspected_uris: list[str] = []

    def recording_inspector(uri: str) -> SourceInspection:
        inspected_uris.append(uri)
        return changed_inspection

    warning_session = StudioSession(
        project=_project,
        source_inspector=recording_inspector,
    )
    inspection = _inspect_with_mtime_warning(warning_session)
    assert inspected_uris == [str(_project.sources[0].uri)]
    assert isinstance(inspection, SourceInspection)
    assert inspection == changed_inspection
    assert _project.sources[0].mtime == 123.0
    report = invoke_preserving_sentinel(
        lambda: _load_canary_or_raise(
            project_path=path,
            source=source,
            adjacent_paths=(adjacent, config, project_stem),
        ),
        owner="gwexpy_studio.persistence.project_io.load_project",
    )
    assert report["status"] == "ok"
    loaded = load_project(path)
    assert loaded.project_id == "project-spike-3"
    assert loaded.sources[0].mtime == 123.0
    assert adjacent.is_file()
    assert config.is_file()
    assert project_stem.is_file()
    assert not marker.exists()
    assert loaded.sources[0].mtime == 123.0
    assert source.read_bytes() == source_snapshot[0]
    assert source.stat().st_mtime_ns == source_snapshot[1]


@pytest.mark.contract("I-S3-003")
def test_future_schema_is_rejected_without_execution(
    tmp_path: Path,
) -> None:
    """A newer schema version fails before any worker or source access."""
    source = tmp_path / "source.h5"
    write_named_hdf5(make_timeseries(random_values()), source)
    adjacent = tmp_path / "adjacent.py"
    config = tmp_path / "config.py"
    project_stem = tmp_path / "future.py"
    marker = tmp_path / "executed.txt"
    for canary_path, label in (
        (adjacent, "adjacent"),
        (config, "config"),
        (project_stem, "stem"),
    ):
        canary_path.write_text(
            f"from pathlib import Path\nPath({str(marker)!r}).write_text({label!r})\n",
            encoding="utf-8",
        )
    canary_snapshots = {
        canary_path: (canary_path.read_bytes(), canary_path.stat().st_mtime_ns)
        for canary_path in (source, adjacent, config, project_stem)
    }
    _project, manifest = _complete_project(source)
    manifest["schema_version"] = 4
    path = tmp_path / "future.gwxproj"
    _write_manifest(path, manifest)
    report = invoke_preserving_sentinel(
        lambda: _load_canary_or_raise(
            project_path=path,
            source=source,
            adjacent_paths=(adjacent, config, project_stem),
            expect_future_schema=True,
        ),
        owner="gwexpy_studio.persistence.project_io.load_project",
    )
    assert report["status"] == "future_rejected"
    assert "schema" in report["message"].lower()

    assert path.is_file()
    assert source.read_bytes() == canary_snapshots[source][0]
    assert not marker.exists()
    for canary_path, (canary_bytes, canary_mtime) in canary_snapshots.items():
        assert canary_path.read_bytes() == canary_bytes
        assert canary_path.stat().st_mtime_ns == canary_mtime


@pytest.mark.contract("I-S3-004")
def test_atomic_save_failure_preserves_old_file_and_modified(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Replacement failure retains old bytes, old modified time, and cleanup."""
    source = tmp_path / "source.h5"
    write_named_hdf5(make_timeseries(random_values()), source)
    project, _manifest = _complete_project(source)
    project.modified = "old-modified"
    path = tmp_path / "project.gwxproj"
    old_bytes = b"old project bytes"
    path.write_bytes(old_bytes)
    old_stat = path.stat()
    replace_calls: list[tuple[Path, Path]] = []
    temporary_paths: list[Path] = []

    def fail_replace(source_path: Any, target_path: Any) -> None:
        source_value = Path(source_path)
        target_value = Path(target_path)
        replace_calls.append((source_value, target_value))
        temporary_paths.append(source_value)
        assert source_value.is_file(), "atomic temp vanished before replace"
        raise OSError("injected replace failure")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError, match="^injected replace failure$"):
        invoke_preserving_sentinel(
            lambda: save_project(
                project,
                path,
                clock=lambda: "new-modified",
            ),
            owner="gwexpy_studio.persistence.project_io.save_project",
        )
    assert len(replace_calls) == 1
    assert replace_calls[0][1] == path
    assert path.read_bytes() == old_bytes
    assert path.stat().st_mtime_ns == old_stat.st_mtime_ns
    assert project.modified == "old-modified"
    assert temporary_paths and all(not value.exists() for value in temporary_paths)
    assert {item.name for item in tmp_path.iterdir()} == {
        "source.h5",
        "project.gwxproj",
    }

    with pytest.raises(OSError, match="^injected replace failure$"):
        save_project(project, path, clock=lambda: "old-modified")
    assert len(replace_calls) == 2
    assert project.modified == "old-modified"
    assert path.read_bytes() == old_bytes
    assert all(not value.exists() for value in temporary_paths)
