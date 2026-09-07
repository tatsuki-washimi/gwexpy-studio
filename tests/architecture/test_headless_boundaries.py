"""Concrete import, schema, and stable-boundary architecture contracts."""

from __future__ import annotations

import ast
import inspect
import json
import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from gwexpy_studio.domain.graph import OperationGraph
from gwexpy_studio.domain.model import DataObjectRef, DataSourceRef, Operation
from gwexpy_studio.domain.project import Project
from gwexpy_studio.errors import PrototypeNotImplementedError
from gwexpy_studio.export.python_exporter import export_python
from gwexpy_studio.ops.timeseries import REGISTRY
from gwexpy_studio.persistence.project_io import load_project, save_project
from gwexpy_studio.plotting.renderer import render_plot
from gwexpy_studio.runtime.executor import execute_operation
from gwexpy_studio.worker import service as service_module
from gwexpy_studio.worker.client import WorkerClient
from tests.support.sentinel import invoke_preserving_sentinel

pytestmark = pytest.mark.architecture

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
PURE_MODULES = (
    "gwexpy_studio.domain.model",
    "gwexpy_studio.domain.graph",
    "gwexpy_studio.domain.project",
    "gwexpy_studio.worker.protocol",
    "gwexpy_studio.worker.client",
    "gwexpy_studio.session",
    "gwexpy_studio.export.python_exporter",
    "gwexpy_studio.persistence.project_io",
)
PURE_FILES = tuple(
    REPOSITORY_ROOT / "src" / Path(module.replace(".", "/") + ".py")
    for module in PURE_MODULES
)
FORBIDDEN_IMPORTS = {"gwexpy", "gwpy", "PySide6", "PyQt6", "qtpy"}

STUDIO_PACKAGE_ROOT = REPOSITORY_ROOT / "src" / "gwexpy_studio"
SCAFFOLD_FORBIDDEN_MODULE_TOKENS = frozenset({"skeleton"})
SCAFFOLD_FORBIDDEN_NAMES = frozenset({"defer"})
SENTINEL_NAME = "PrototypeNotImplementedError"
SENTINEL_ALLOWED_FILES = frozenset(
    {STUDIO_PACKAGE_ROOT / "errors.py", STUDIO_PACKAGE_ROOT / "__init__.py"}
)
MINIMUM_SCANNED_PRODUCTION_MODULES = 20


def _scaffold_module_tokens(value: str) -> set[str]:
    """Normalize a dotted import path the same way the source tripwire does."""
    return {part.strip("_").lower() for part in value.split(".") if part}


def _import_roots(tree: ast.AST) -> set[str]:
    """Return imported root names from a parsed source tree."""
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".", 1)[0])
    return roots


def _fresh_process_environment() -> dict[str, str]:
    """Make the installed source tree available to a clean child process."""
    environment = os.environ.copy()
    source_paths = [str(REPOSITORY_ROOT / "src"), str(REPOSITORY_ROOT)]
    existing = environment.get("PYTHONPATH")
    if existing:
        source_paths.append(existing)
    environment["PYTHONPATH"] = os.pathsep.join(source_paths)
    return environment


class _ArchitectureShutdownConnection:
    """Pickleable child endpoint used only by the fresh worker probe."""

    def __init__(self) -> None:
        self.closed = False

    def recv_bytes(self) -> bytes:
        return json.dumps(
            {
                "protocol": 2,
                "request_id": "00000000-0000-4000-8000-000000000201",
                "type": "shutdown",
                "payload": {},
            }
        ).encode("utf-8")

    def send_bytes(self, _value: bytes) -> None:
        return None

    def close(self) -> None:
        self.closed = True


def _architecture_worker_probe(report_connection: Any, parent_pid: int) -> None:
    """Run the worker entry point in a spawn-importable fresh child."""
    from gwexpy_studio.worker.service import worker_main

    forbidden = ("gwexpy", "gwpy", "PySide6", "PyQt6", "qtpy")
    before = {name: name in sys.modules for name in forbidden}
    endpoint = _ArchitectureShutdownConnection()
    report: dict[str, Any] = {
        "child_pid": os.getpid(),
        "parent_pid": os.getppid(),
        "expected_parent_pid": parent_pid,
        "before": before,
    }
    try:
        worker_main(endpoint)
    except Exception as error:
        if type(error).__name__ == "PrototypeNotImplementedError":
            report.update(
                {
                    "status": "sentinel",
                    "code": getattr(error, "code"),
                    "owner": getattr(error, "owner"),
                }
            )
        else:
            raise
    else:
        report["status"] = "ok"
    finally:
        report["worker_connection_closed"] = endpoint.closed
        report["after"] = {name: name in sys.modules for name in forbidden}
        endpoint.close()
        report_connection.send_bytes(json.dumps(report).encode("utf-8"))
        report_connection.close()


def _architecture_write_source(path: str) -> None:
    """Create the named HDF5 fixture from a spawn-importable child."""
    from tests.support.fixtures import make_timeseries, random_values, write_named_hdf5

    write_named_hdf5(make_timeseries(random_values()), Path(path))


def _fresh_worker_import_probe() -> dict[str, Any]:
    """Run the spawn child inside an externally supervised fresh process."""
    import multiprocessing

    context = multiprocessing.get_context("spawn")
    parent_connection, child_connection = context.Pipe(duplex=False)
    child = context.Process(
        target=_architecture_worker_probe,
        args=(child_connection, os.getpid()),
    )
    try:
        child.start()
        child_connection.close()
        assert parent_connection.poll(15.0), "worker import probe timed out"
        report = json.loads(parent_connection.recv_bytes().decode("utf-8"))
        child.join(timeout=5.0)
        exitcode = child.exitcode
        if child.is_alive():
            child.terminate()
            child.join(timeout=5.0)
        if child.is_alive():
            child.kill()
            child.join(timeout=5.0)
        assert exitcode == 0
        return report
    finally:
        if child.is_alive():
            child.terminate()
            child.join(timeout=5.0)
        if child.is_alive():
            child.kill()
            child.join(timeout=5.0)
        child.close()
        parent_connection.close()
        child_connection.close()


def _assert_fresh_parent_probe_is_scientific_free() -> None:
    """Prove the parent-side import boundary in an independent interpreter."""
    probe = """
import sys
import gwexpy_studio.worker.service
assert 'gwexpy' not in sys.modules
assert 'gwpy' not in sys.modules
assert 'PySide6' not in sys.modules
assert 'PyQt6' not in sys.modules
assert 'qtpy' not in sys.modules
"""
    completed = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=REPOSITORY_ROOT / "tests",
        env=_fresh_process_environment(),
        capture_output=True,
        text=False,
        check=False,
        timeout=15,
    )
    assert completed.returncode == 0, completed.stderr.decode("utf-8", errors="replace")


def _fresh_named_source(tmp_path: Path) -> Path:
    """Prepare HDF5 in a supervised process whose child owns Process.start."""
    source = tmp_path / "architecture-parent-probe.h5"
    probe = r"""
import multiprocessing
import sys

def write_source(path):
    from tests.support.fixtures import make_timeseries, random_values, write_named_hdf5
    write_named_hdf5(make_timeseries(random_values()), path)

def finish_child(child, started):
    if not started:
        return
    if child.is_alive():
        child.terminate()
        child.join(timeout=5.0)
    if child.is_alive():
        child.kill()
        child.join(timeout=5.0)
    assert not child.is_alive(), "HDF5 writer child leaked"
    child.close()

def main():
    from tests.architecture.test_headless_boundaries import _architecture_write_source

    context = multiprocessing.get_context("spawn")
    child = context.Process(target=_architecture_write_source, args=(sys.argv[1],))
    started = False
    try:
        child.start()
        started = True
        child.join(timeout=30.0)
        exitcode = child.exitcode
        finish_child(child, started)
        started = False
        assert exitcode == 0
        print("ok")
    finally:
        finish_child(child, started)

main()
"""
    output = _run_bounded_probe(probe, str(source))
    assert output.decode("utf-8").splitlines()[-1] == "ok"
    assert source.is_file()
    return source


def _run_bounded_probe(probe: str, *arguments: str) -> bytes:
    """Run a probe in a new process group with a strict external watchdog."""
    child = subprocess.Popen(
        [sys.executable, "-c", probe, *arguments],
        cwd=REPOSITORY_ROOT / "tests",
        env=_fresh_process_environment(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        stdout, stderr = child.communicate(timeout=20.0)
    except subprocess.TimeoutExpired as error:
        try:
            os.killpg(child.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            child.wait(timeout=3.0)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            child.wait(timeout=3.0)
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        assert child.poll() is not None, "supervisor survived timeout cleanup"
        raise AssertionError("fresh parent replay probe exceeded watchdog") from error
    assert child.returncode == 0, stderr.decode("utf-8", errors="replace")
    return stdout


def _fresh_parent_session_replay_probe(source: Path) -> None:
    """Run a real named-HDF5 graph/replay workflow only in a fresh process."""
    probe = r"""
import json
import multiprocessing
import os
import sys
import time

forbidden = ("gwexpy", "gwpy", "PySide6", "PyQt6", "qtpy")
before = {name: name in sys.modules for name in forbidden}
from gwexpy_studio.domain.graph import OperationGraph
from gwexpy_studio.domain.model import DataSourceRef, Operation
from gwexpy_studio.domain.project import Project
from gwexpy_studio.errors import PrototypeNotImplementedError
from gwexpy_studio.session import StudioSession
from gwexpy_studio.worker.client import WorkerClient
from gwexpy_studio.worker.service import worker_main

source = sys.argv[1]

class ProbeGraph(OperationGraph):
    def __init__(self, operations):
        self._operations = tuple(operations)

    @property
    def operations(self):
        return self._operations

    def producer_of(self, object_id):
        return next(
            (
                operation
                for operation in self._operations
                if object_id in operation.outputs
            ),
            None,
        )

    def ancestors(self, target_object_ids):
        selected = set()
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

graph = ProbeGraph(
    (
        Operation(
            op_id="op-1",
            operation_id="timeseries.read",
            operation_schema=1,
            params={"source": source, "format": "hdf5", "name": "X1:STUDIO-TEST"},
            outputs=("obj-1",),
        ),
        Operation(
            op_id="op-2",
            operation_id="timeseries.asd",
            operation_schema=1,
            inputs={"self": "obj-1"},
            params={"fftlength": {"value": 4.0, "unit": "s"}},
            outputs=("obj-2",),
        ),
    )
)
project = Project(
    project_id="architecture-parent-probe",
    sources=(
        DataSourceRef(
            source_id="src-001",
            uri=source,
            format="hdf5",
            size_bytes=os.stat(source).st_size,
            mtime=os.stat(source).st_mtime,
        ),
    ),
    graph=graph,
)
context = multiprocessing.get_context("spawn")
client = WorkerClient(
    context=context,
    process_factory=context.Process,
    connection_factory=context.Pipe,
    worker_target=worker_main,
    startup_timeout_s=5.0,
    request_timeout_s=5.0,
    join_timeout_s=5.0,
)
session = StudioSession(project=project, graph=graph, client=client)
baseline_children = {
    child.pid for child in multiprocessing.active_children() if child.pid is not None
}
sentinels = []
materialized = False
worker_pid = None
try:
    try:
        session.start()
    except PrototypeNotImplementedError as error:
        sentinels.append({"code": error.code, "owner": error.owner})
    else:
        try:
            replay_result = session.replay(targets=("obj-2",))
        except PrototypeNotImplementedError as error:
            sentinels.append({"code": error.code, "owner": error.owner})
        else:
            assert replay_result, "fresh replay returned no result"
            replay_payload = (
                replay_result.get("payload", replay_result)
                if isinstance(replay_result, dict)
                else replay_result
            )
            returned_ids = (
                replay_payload.get("object_ids", replay_payload.get("object_id"))
                if isinstance(replay_payload, dict)
                else None
            )
            if isinstance(returned_ids, str):
                returned_ids = (returned_ids,)
            assert tuple(returned_ids or ()) == ("obj-2",)
            assert tuple(value.object_id for value in project.objects) == (
                "obj-1",
                "obj-2",
            )
            assert tuple(record.op_id for record in project.executions) == (
                "op-1",
                "op-2",
            )
            assert all(
                record.status == "succeeded" for record in project.executions
            )
            ping = client.request({
                "protocol": 2,
                "request_id": "00000000-0000-4000-8000-000000000202",
                "type": "ping",
                "payload": {},
            })
            assert ping["type"] == "result"
            worker_pid = ping["payload"]["pid"]
            assert worker_pid != os.getpid()
            listed = client.request({
                "protocol": 2,
                "request_id": "00000000-0000-4000-8000-000000000203",
                "type": "list_objects",
                "payload": {},
            })
            assert listed["type"] == "result"
            assert tuple(listed["payload"]["object_ids"]) == (
                "obj-1",
                "obj-2",
            )
            materialized = True
finally:
    try:
        session.close()
    except PrototypeNotImplementedError as error:
        sentinels.append({"code": error.code, "owner": error.owner})
    if materialized:
        assert client.state.name == "CLOSED"
    else:
        try:
            client.shutdown()
        except PrototypeNotImplementedError as error:
            sentinels.append({"code": error.code, "owner": error.owner})
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        current = {
            child.pid
            for child in multiprocessing.active_children()
            if child.pid is not None
        }
        if current <= baseline_children:
            break
        time.sleep(0.05)
    assert {
        child.pid
        for child in multiprocessing.active_children()
        if child.pid is not None
    } <= baseline_children

after = {name: name in sys.modules for name in forbidden}
assert not any(after.values()), (before, after)
print(json.dumps({
    "before": before,
    "after": after,
    "materialized": materialized,
    "worker_pid": worker_pid,
    "sentinels": sentinels,
}))
"""
    output = _run_bounded_probe(probe, str(source))
    report = json.loads(output.decode("utf-8").splitlines()[-1])
    assert report["after"] == {
        "gwexpy": False,
        "gwpy": False,
        "PySide6": False,
        "PyQt6": False,
        "qtpy": False,
    }
    if report["materialized"]:
        assert report["worker_pid"] != os.getpid()
    else:
        assert report["sentinels"]
        assert all(
            item["code"] == "STUDIO-FOUNDATION-NOT-IMPLEMENTED"
            for item in report["sentinels"]
        )
        assert any(
            item["owner"] == "gwexpy_studio.session.StudioSession.start"
            for item in report["sentinels"]
        )


@pytest.mark.contract("I-ARCH-001")
def test_pure_boundary_modules_import_without_gwexpy_or_qt_in_fresh_process() -> None:
    """Pure Studio boundaries stay free of scientific and Qt imports."""
    for path in PURE_FILES:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        assert _import_roots(tree).isdisjoint(FORBIDDEN_IMPORTS), path

    module_literals = ", ".join(repr(module) for module in PURE_MODULES)
    probe = f"""
import importlib
import sys
for module in ({module_literals},):
    importlib.import_module(module)
assert 'gwexpy' not in sys.modules
assert 'gwpy' not in sys.modules
assert 'PySide6' not in sys.modules
assert 'PyQt6' not in sys.modules
assert 'qtpy' not in sys.modules
"""
    completed = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=REPOSITORY_ROOT / "tests",
        env=_fresh_process_environment(),
        capture_output=True,
        text=False,
        check=False,
        timeout=15,
    )
    assert completed.returncode == 0, completed.stderr.decode("utf-8", errors="replace")


@pytest.mark.contract("I-ARCH-002")
def test_domain_sources_have_no_qt_imports_and_project_is_data_only() -> None:
    """Domain and persistence source code enforce the data-only boundary."""
    domain_paths = tuple((REPOSITORY_ROOT / "src/gwexpy_studio/domain").glob("*.py"))
    for path in domain_paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        assert _import_roots(tree).isdisjoint({"PySide6", "PyQt6", "qtpy"}), path

    project_io = (
        REPOSITORY_ROOT / "src/gwexpy_studio/persistence/project_io.py"
    ).read_text(encoding="utf-8")
    assert "pickle" not in project_io
    assert "yaml" not in project_io
    assert "subprocess" not in project_io
    assert "multiprocessing" not in project_io

    schema = json.loads(
        (
            REPOSITORY_ROOT / "src/gwexpy_studio/schemas/project-v1.schema.json"
        ).read_text(encoding="utf-8")
    )
    assert schema["$schema"].endswith("draft/2020-12/schema")
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {
        "schema_version",
        "project_id",
        "created",
        "modified",
        "compatibility",
        "sources",
        "objects",
        "operations",
        "executions",
        "plots",
        "ui_state",
    }
    assert "pickle" not in json.dumps(schema).lower()


@pytest.mark.contract("I-ARCH-003")
def test_fixture_factory_imports_gwexpy_only_when_called() -> None:
    """Fixture support imports gwexpy lazily at the scientific construction seam."""
    probe = """
import sys
from tests.support.fixtures import make_timeseries, random_values
assert 'gwexpy' not in sys.modules
series = make_timeseries(random_values())
assert type(series).__name__ == 'TimeSeries'
assert 'gwexpy' in sys.modules
"""
    completed = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=REPOSITORY_ROOT / "tests",
        env=_fresh_process_environment(),
        capture_output=True,
        text=False,
        check=False,
        timeout=20,
    )
    assert completed.returncode == 0, completed.stderr.decode("utf-8", errors="replace")


@pytest.mark.contract("I-ARCH-004")
def test_worker_entrypoint_keeps_gwexpy_inside_scientific_child(
    tmp_path: Path,
) -> None:
    """Worker startup owns the lazy scientific import and no parent import."""
    source = _fresh_named_source(tmp_path)
    _assert_fresh_parent_probe_is_scientific_free()
    _fresh_parent_session_replay_probe(source)
    report = _fresh_worker_import_probe()
    if report["status"] == "sentinel":

        def raise_child_sentinel() -> None:
            raise PrototypeNotImplementedError(
                code=report["code"], owner=report["owner"]
            )

        invoke_preserving_sentinel(
            raise_child_sentinel,
            owner="gwexpy_studio.worker.service.worker_main",
        )

    source_text = inspect.getsource(service_module.worker_main)
    function = ast.parse(source_text).body[0]
    assert isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef))
    local_imports = _import_roots(function)
    assert "gwexpy" in local_imports or "gwpy" in local_imports
    assert report["child_pid"] != os.getpid()
    assert report["parent_pid"] == report["expected_parent_pid"] == os.getpid()
    assert report["before"]["gwexpy"] is False
    assert report["before"]["gwpy"] is False
    assert report["after"]["gwexpy"] or report["after"]["gwpy"]
    assert report["after"]["PySide6"] is False
    assert report["after"]["PyQt6"] is False
    assert report["after"]["qtpy"] is False
    assert report["worker_connection_closed"] is True
    _assert_fresh_parent_probe_is_scientific_free()
    _fresh_parent_session_replay_probe(source)


@pytest.mark.contract("I-ARCH-005")
def test_preview_stride_is_display_only_at_public_boundaries() -> None:
    """Preview slicing is absent from science, export, and persistence APIs."""
    science_source = inspect.getsource(execute_operation)
    export_source = inspect.getsource(export_python)
    save_source = inspect.getsource(save_project)
    load_source = inspect.getsource(load_project)
    assert "preview_stride" not in science_source
    assert "preview_stride" not in export_source
    assert "preview_stride" not in save_source
    assert "preview_stride" not in load_source

    display_sources = (
        inspect.getsource(WorkerClient.get_array),
        inspect.getsource(render_plot),
    )
    assert all("preview_stride" in source for source in display_sources)


@pytest.mark.contract("I-ARCH-006")
def test_operation_graph_and_stable_api_declarations_are_source_of_truth() -> None:
    """The graph/export API is explicit before deep execution is implemented."""
    invoke_preserving_sentinel(
        lambda: export_python(Project(), deterministic=True),
        owner="gwexpy_studio.export.python_exporter.export_python",
    )

    export_signature = inspect.signature(export_python)
    assert tuple(export_signature.parameters) == (
        "project",
        "targets",
        "value_dumps",
        "deterministic",
        "include_data_writes",
    )
    graph_source = inspect.getsource(OperationGraph)
    graph_tree = ast.parse(graph_source)
    graph_methods = {
        node.name
        for node in ast.walk(graph_tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert {"producer_of", "ancestors"} <= graph_methods
    assert tuple(REGISTRY) == (
        "timeseries.read",
        "timeseries.crop",
        "timeseries.detrend",
        "timeseries.asd",
        "timeseries.spectrogram",
    )

    class GraphSpy:
        """Record the exporter’s live graph traversal, not just its AST shape."""

        def __init__(self, operations: tuple[Operation, ...]) -> None:
            self.operations = operations
            self.calls: list[tuple[str, tuple[str, ...]]] = []

        def ancestors(self, targets: Any) -> tuple[Operation, ...]:
            normalized = tuple(targets)
            self.calls.append(("ancestors", normalized))
            selected: set[str] = set()
            pending = list(normalized)
            while pending:
                object_id = pending.pop()
                operation = self.producer_of(object_id)
                if operation is None or operation.op_id in selected:
                    continue
                selected.add(operation.op_id)
                pending.extend(operation.inputs.values())
            return tuple(
                operation
                for operation in self.operations
                if operation.op_id in selected
            )

        def producer_of(self, object_id: str) -> Operation | None:
            self.calls.append(("producer_of", (object_id,)))
            return next(
                (item for item in self.operations if object_id in item.outputs),
                None,
            )

    operations = (
        Operation(
            op_id="op-1",
            operation_id="timeseries.read",
            operation_schema=1,
            params={"source": "/tmp/graph-spy.h5", "format": "hdf5"},
            outputs=("obj-1",),
        ),
        Operation(
            op_id="op-2",
            operation_id="timeseries.crop",
            operation_schema=1,
            inputs={"self": "obj-1"},
            params={"start": 0.0, "end": 1.0},
            outputs=("obj-2",),
        ),
        Operation(
            op_id="op-3",
            operation_id="timeseries.asd",
            operation_schema=1,
            inputs={"self": "obj-2"},
            params={"fftlength": {"value": 1.0, "unit": "s"}},
            outputs=("obj-3",),
        ),
        Operation(
            op_id="op-4",
            operation_id="timeseries.spectrogram",
            operation_schema=1,
            inputs={"self": "obj-2"},
            params={
                "stride": {"value": 1.0, "unit": "s"},
                "fftlength": {"value": 1.0, "unit": "s"},
            },
            outputs=("obj-4",),
        ),
        Operation(
            op_id="op-5",
            operation_id="timeseries.unknown",
            operation_schema=1,
            inputs={"self": "obj-2"},
            params={},
            outputs=("obj-5",),
        ),
    )
    graph_spy = GraphSpy(operations)
    spy_project = Project(
        project_id="graph-spy",
        sources=(
            DataSourceRef(
                source_id="src-1",
                uri="/tmp/graph-spy.h5",
                format="hdf5",
                size_bytes=None,
                mtime=None,
            ),
        ),
        objects=(
            DataObjectRef(
                object_id="obj-1",
                kind="TimeSeries",
                shape=(2,),
                dtype="float64",
                unit="m",
                axes={
                    "t0": {"value": 0.0, "unit": "s"},
                    "dt": {"value": 1.0, "unit": "s"},
                },
                produced_by=None,
            ),
            DataObjectRef(
                object_id="obj-2",
                kind="TimeSeries",
                shape=(2,),
                dtype="float64",
                unit="m",
                axes={
                    "t0": {"value": 0.0, "unit": "s"},
                    "dt": {"value": 1.0, "unit": "s"},
                },
                produced_by="op-2",
            ),
            DataObjectRef(
                object_id="obj-3",
                kind="FrequencySeries",
                shape=(2,),
                dtype="float64",
                unit="m / Hz(1/2)",
                axes={
                    "f0": {"value": 0.0, "unit": "Hz"},
                    "df": {"value": 1.0, "unit": "Hz"},
                },
                produced_by="op-3",
            ),
            DataObjectRef(
                object_id="obj-4",
                kind="Spectrogram",
                shape=(2, 2),
                dtype="float64",
                unit="m",
                axes={
                    "t0": {"value": 0.0, "unit": "s"},
                    "dt": {"value": 1.0, "unit": "s"},
                    "f0": {"value": 0.0, "unit": "Hz"},
                    "df": {"value": 1.0, "unit": "Hz"},
                },
                produced_by="op-4",
            ),
        ),
        graph=graph_spy,
    )
    generated = export_python(
        spy_project,
        deterministic=True,
    )
    assert graph_spy.calls
    default_traversed_targets = {
        target
        for method, targets in graph_spy.calls
        if method == "ancestors"
        for target in targets
    }
    assert default_traversed_targets == {"obj-3", "obj-4"}
    assert "timeseries.read" in generated
    assert "timeseries.crop" in generated
    assert "timeseries.asd" in generated
    assert "timeseries.spectrogram" in generated
    assert "timeseries.unknown" not in generated
    graph_spy.calls.clear()
    explicit = export_python(
        spy_project,
        targets=("obj-4",),
        deterministic=True,
    )
    explicit_traversed_targets = {
        target
        for method, targets in graph_spy.calls
        if method == "ancestors"
        for target in targets
    }
    assert explicit_traversed_targets == {"obj-4"}
    assert "timeseries.spectrogram" in explicit
    assert "timeseries.asd" not in explicit


@pytest.mark.contract("I-ARCH-007")
def test_no_production_module_reintroduces_scaffold_deferral() -> None:
    """No src module imports a skeleton helper, names defer, or raises the sentinel."""
    scanned = 0
    for path in sorted(STUDIO_PACKAGE_ROOT.rglob("*.py")):
        scanned += 1
        text = path.read_text(encoding="utf-8")
        tree = ast.parse(text, filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert SCAFFOLD_FORBIDDEN_MODULE_TOKENS.isdisjoint(
                        _scaffold_module_tokens(alias.name)
                    ), f"{path}:{node.lineno} imports {alias.name}"
            elif isinstance(node, ast.ImportFrom):
                assert SCAFFOLD_FORBIDDEN_MODULE_TOKENS.isdisjoint(
                    _scaffold_module_tokens(node.module or "")
                ), f"{path}:{node.lineno} imports from {node.module}"
                for alias in node.names:
                    # `from . import skeleton` / `from .. import skeleton`
                    # puts the forbidden module name in alias.name, not
                    # node.module (node.module is None for a bare relative
                    # import). Treat alias.name as a module token too, in
                    # addition to the existing forbidden-name check.
                    assert SCAFFOLD_FORBIDDEN_MODULE_TOKENS.isdisjoint(
                        _scaffold_module_tokens(alias.name)
                    ), f"{path}:{node.lineno} imports {alias.name}"
                    assert alias.name not in SCAFFOLD_FORBIDDEN_NAMES, (
                        f"{path}:{node.lineno} imports {alias.name}"
                    )
            elif isinstance(node, ast.Name):
                assert node.id not in SCAFFOLD_FORBIDDEN_NAMES, (
                    f"{path}:{node.lineno} names {node.id}"
                )
            elif isinstance(node, ast.Attribute):
                assert node.attr not in SCAFFOLD_FORBIDDEN_NAMES, (
                    f"{path}:{node.lineno} calls .{node.attr}"
                )
            elif isinstance(node, ast.FunctionDef):
                assert node.name not in SCAFFOLD_FORBIDDEN_NAMES, (
                    f"{path}:{node.lineno} defines {node.name}"
                )
        if path not in SENTINEL_ALLOWED_FILES:
            assert SENTINEL_NAME not in text, f"{path} references {SENTINEL_NAME}"
    assert scanned >= MINIMUM_SCANNED_PRODUCTION_MODULES, scanned
