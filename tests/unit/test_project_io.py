"""Contract tests for strict project JSON validation and atomic persistence."""

from __future__ import annotations

import builtins
import copy
import io
import json
import multiprocessing
import os
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from gwexpy_studio.domain.project import Project
from gwexpy_studio.errors import ProjectFormatError
from gwexpy_studio.persistence.project_io import (
    load_project,
    save_project,
    validate_project_document,
)
from gwexpy_studio.session import StudioSession
from gwexpy_studio.worker.client import WorkerClient

SCHEMA_PATH = (
    Path(__file__).parents[2]
    / "src"
    / "gwexpy_studio"
    / "schemas"
    / "project-v1.schema.json"
)


def _manifest() -> dict[str, Any]:
    """Return a small but complete deterministic project manifest."""
    return {
        "schema_version": 1,
        "project_id": "project-io",
        "created": "2026-08-16T00:00:00Z",
        "modified": "2026-08-16T00:00:01Z",
        "compatibility": {"studio": "0.1.0", "gwexpy": "0.1.14"},
        "sources": [
            {
                "source_id": "src-1",
                "uri": "/data/source.h5",
                "format": "hdf5",
                "size_bytes": 42,
                "mtime": 123.0,
            }
        ],
        "objects": [
            {
                "object_id": "obj-1",
                "kind": "TimeSeries",
                "shape": [15360],
                "dtype": "float64",
                "unit": "m",
                "name": "X1:STUDIO-TEST",
                "channel": "X1:STUDIO-CHANNEL",
                "axes": {
                    "t0": {"value": 1_000_000_000.0, "unit": "s"},
                    "dt": {"value": 0.00390625, "unit": "s"},
                },
                "produced_by": None,
            },
            {
                "object_id": "obj-2",
                "kind": "FrequencySeries",
                "shape": [7681],
                "dtype": "float64",
                "unit": "m / Hz**0.5",
                "name": "X1:STUDIO-TEST-asd",
                "channel": "X1:STUDIO-CHANNEL",
                "axes": {
                    "f0": {"value": 0.0, "unit": "Hz"},
                    "df": {"value": 0.25, "unit": "Hz"},
                },
                "produced_by": "op-1",
            },
        ],
        "operations": [
            {
                "op_id": "op-1",
                "operation_id": "timeseries.asd",
                "operation_schema": 1,
                "inputs": {"self": "obj-1"},
                "params": {"fftlength": {"value": 4.0, "unit": "s"}},
                "outputs": ["obj-2"],
            }
        ],
        "executions": [
            {
                "execution_id": "exec-1",
                "op_id": "op-1",
                "started_at": "2026-08-16T00:00:00Z",
                "duration_s": 1.0,
                "status": "succeeded",
                "warnings": [],
                "error": None,
                "environment": {"python": "3.12.12", "gwexpy": "0.1.14"},
            }
        ],
        "plots": [
            {
                "plot_id": "plot-1",
                "kind": "line",
                "object_ids": ["obj-1", "obj-2"],
                "xscale": "linear",
                "yscale": "log",
                "xlim": [0.0, 60.0],
                "ylim": [1e-12, 1.0],
                "title": "project io",
                "xlabel": "GPS seconds",
                "ylabel": "amplitude",
                "legend": True,
                "styles": {"color": "black"},
            }
        ],
        "ui_state": {"selected_object": "obj-2"},
    }


def _schema() -> dict[str, Any]:
    """Load the repository schema as JSON data."""
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def _write_manifest(path: Path, document: Mapping[str, Any]) -> None:
    """Write one deterministic UTF-8 project document."""
    path.write_text(
        json.dumps(document, ensure_ascii=False, sort_keys=True), encoding="utf-8"
    )


def _json_depth(value: Any) -> int:
    """Return the structural JSON nesting depth of one data-only value."""
    if isinstance(value, Mapping):
        return 1 + max((_json_depth(item) for item in value.values()), default=0)
    if isinstance(value, list):
        return 1 + max((_json_depth(item) for item in value), default=0)
    return 0


def _manifest_at_depth(depth: int) -> dict[str, Any]:
    """Build an otherwise-valid manifest with an exact structural depth."""
    nested: Any = "leaf"
    while True:
        document = copy.deepcopy(_manifest())
        document["ui_state"] = {"nested": nested}
        current_depth = _json_depth(document)
        if current_depth == depth:
            return document
        if current_depth > depth:
            raise AssertionError(f"manifest base exceeds requested depth {depth}")
        nested = {"level": nested}


def _expect_project_format_error(callback: Callable[[], Any]) -> None:
    """Accept only the public format error once implementation exists."""
    try:
        callback()
    except ProjectFormatError:
        return
    raise AssertionError("invalid project input was accepted")


@pytest.mark.contract("C-A-040")
def test_project_schema_accepts_a_complete_v1_manifest() -> None:
    """The executable Draft 2020-12 schema accepts the representative manifest."""
    schema = _schema()
    validator = Draft202012Validator(schema)

    Draft202012Validator.check_schema(schema)
    assert list(validator.iter_errors(_manifest())) == []


@pytest.mark.contract("C-A-003")
def test_project_schema_enforces_stable_id_formats() -> None:
    """The production schema accepts natural growth and rejects malformed IDs."""
    validator = Draft202012Validator(_schema())
    grown = copy.deepcopy(_manifest())
    grown["sources"][0]["source_id"] = "src-999"
    grown["objects"][0]["object_id"] = "obj-999"
    grown["operations"][0]["op_id"] = "op-999"
    grown["executions"][0]["execution_id"] = "exec-999"
    grown["plots"][0]["plot_id"] = "plot-999"

    assert validator.is_valid(grown)

    invalid_cases = (
        ("source", "sources", "source_id", "src-0"),
        ("object", "objects", "object_id", "obj-01"),
        ("operation", "operations", "op_id", "op-0"),
        ("execution", "executions", "execution_id", "exec-"),
        ("plot", "plots", "plot_id", "plot-x"),
    )
    for label, collection, field, value in invalid_cases:
        candidate = copy.deepcopy(_manifest())
        candidate[collection][0][field] = value
        assert not validator.is_valid(candidate), label


@pytest.mark.parametrize(
    "document",
    [
        pytest.param(
            {**copy.deepcopy(_manifest()), "schema_version": 3},
            id="future-schema",
            marks=pytest.mark.contract("C-A-041"),
        ),
        pytest.param(
            {
                **copy.deepcopy(_manifest()),
                "sources": [
                    {
                        **copy.deepcopy(_manifest())["sources"][0],
                        "format": "fits",
                    }
                ],
            },
            id="unknown-source-format",
            marks=pytest.mark.contract("C-A-042"),
        ),
        pytest.param(
            {
                **copy.deepcopy(_manifest()),
                "objects": [
                    {
                        **copy.deepcopy(_manifest())["objects"][0],
                        "kind": "Spectrogram",
                    },
                    copy.deepcopy(_manifest())["objects"][1],
                ],
            },
            id="kind-specific-axis-mismatch",
            marks=pytest.mark.contract("C-A-043"),
        ),
        pytest.param(
            {
                **copy.deepcopy(_manifest()),
                "objects": [
                    {
                        **copy.deepcopy(_manifest())["objects"][0],
                        "unexpected": "reject-me",
                    },
                    copy.deepcopy(_manifest())["objects"][1],
                ],
            },
            id="unknown-object-field",
            marks=pytest.mark.contract("C-A-044"),
        ),
        pytest.param(
            {
                **copy.deepcopy(_manifest()),
                "sources": [
                    {
                        **copy.deepcopy(_manifest())["sources"][0],
                        "source_id": "src-0",
                    }
                ],
            },
            id="invalid-id",
            marks=pytest.mark.contract("C-A-045"),
        ),
        pytest.param(
            {
                **copy.deepcopy(_manifest()),
                "objects": [
                    {
                        **copy.deepcopy(_manifest())["objects"][0],
                        "shape": [True],
                    },
                    copy.deepcopy(_manifest())["objects"][1],
                ],
            },
            id="bool-as-int",
            marks=pytest.mark.contract("C-A-046"),
        ),
    ],
)
def test_project_schema_rejects_mutation_corpus(document: Mapping[str, Any]) -> None:
    """Schema-only mutation cases fail before strict object reconstruction."""
    validator = Draft202012Validator(_schema())

    assert not validator.is_valid(document)


@pytest.mark.parametrize(
    "document",
    [
        pytest.param(
            {
                **copy.deepcopy(_manifest()),
                "operations": [
                    {
                        **copy.deepcopy(_manifest())["operations"][0],
                        "params": {"value": float("nan")},
                    }
                ],
            },
            id="nan",
            marks=pytest.mark.contract("C-A-050"),
        ),
        pytest.param(
            {
                **copy.deepcopy(_manifest()),
                "operations": [
                    {
                        **copy.deepcopy(_manifest())["operations"][0],
                        "params": {"value": float("inf")},
                    }
                ],
            },
            id="positive-infinity",
            marks=pytest.mark.contract("C-A-051"),
        ),
        pytest.param(
            {
                **copy.deepcopy(_manifest()),
                "objects": [
                    {
                        **copy.deepcopy(_manifest())["objects"][0],
                        "shape": [True],
                    },
                    copy.deepcopy(_manifest())["objects"][1],
                ],
            },
            id="bool-as-int",
            marks=pytest.mark.contract("C-A-052"),
        ),
    ],
)
def test_validate_project_document_rejects_strict_parser_mutations(
    document: Mapping[str, Any],
) -> None:
    """The persistence owner rejects non-finite and bool-as-int values."""
    _expect_project_format_error(lambda: validate_project_document(dict(document)))


@pytest.mark.contract("C-A-060")
def test_load_rejects_duplicate_json_keys(tmp_path: Path) -> None:
    """A duplicate in an otherwise-valid document is rejected explicitly."""
    path = tmp_path / "duplicate.gwxproj"
    valid_raw = json.dumps(_manifest(), ensure_ascii=False, sort_keys=True)
    duplicate_raw = valid_raw[:-1] + ', "project_id": "project-io"}'
    parsed_last_value = json.loads(duplicate_raw)
    assert Draft202012Validator(_schema()).is_valid(parsed_last_value)
    path.write_text(duplicate_raw, encoding="utf-8")

    _expect_project_format_error(lambda: load_project(path))


@pytest.mark.parametrize(
    "constant",
    [
        pytest.param("NaN", id="nan", marks=pytest.mark.contract("C-A-061")),
        pytest.param(
            "Infinity", id="positive-infinity", marks=pytest.mark.contract("C-A-062")
        ),
        pytest.param(
            "-Infinity", id="negative-infinity", marks=pytest.mark.contract("C-A-063")
        ),
    ],
)
def test_load_rejects_nonfinite_json_constants(tmp_path: Path, constant: str) -> None:
    """JSON constants outside finite numbers never enter the domain model."""
    raw = json.dumps(_manifest(), sort_keys=True).replace("4.0", constant, 1)
    path = tmp_path / "nonfinite.gwxproj"
    path.write_text(raw, encoding="utf-8")

    _expect_project_format_error(lambda: load_project(path))


@pytest.mark.contract("C-A-064")
def test_load_rejects_a_project_over_the_size_limit(tmp_path: Path) -> None:
    """Project bytes are bounded before untrusted JSON is reconstructed."""
    path = tmp_path / "oversized.gwxproj"
    oversized = '{"padding":"' + ("x" * (16 * 1024 * 1024)) + '"}'
    path.write_text(oversized, encoding="utf-8")

    _expect_project_format_error(lambda: load_project(path))


@pytest.mark.parametrize(
    "depth",
    [
        pytest.param(64, id="depth-64", marks=pytest.mark.contract("C-A-074")),
        pytest.param(65, id="depth-65", marks=pytest.mark.contract("C-A-065")),
    ],
)
def test_load_enforces_manifest_depth_boundary(tmp_path: Path, depth: int) -> None:
    """The exact structural depth limit is accepted once and rejected above it."""
    document = _manifest_at_depth(depth)
    assert _json_depth(document) == depth
    path = tmp_path / "deep.gwxproj"
    _write_manifest(path, document)

    if depth == 64:
        loaded = load_project(path)
        assert loaded.project_id == "project-io"
    else:
        _expect_project_format_error(lambda: load_project(path))


def _iterative_json_depth(value: Any) -> int:
    """Return structural JSON depth using an explicit stack.

    ``_json_depth`` above is recursive and would itself raise
    ``RecursionError`` well before reaching the depths (4000, 20000)
    exercised below. This mirrors
    ``persistence.project_io._bounded_json_depth``'s own algorithm
    independently, purely to size test fixtures safely; it is not a
    substitute for exercising the production iterative walk.
    """
    if not isinstance(value, (dict, list)):
        return 0
    max_depth = 1
    stack: list[tuple[Any, int]] = [(value, 1)]
    while stack:
        node, level = stack.pop()
        children = node.values() if isinstance(node, dict) else node
        for child in children:
            if isinstance(child, (dict, list)):
                child_level = level + 1
                max_depth = max(max_depth, child_level)
                stack.append((child, child_level))
    return max_depth


def _raw_deep_manifest_bytes(depth: int) -> bytes:
    """Serialize an otherwise-valid manifest with ``ui_state`` nested to ``depth``.

    Builds the nested JSON text directly via string concatenation instead of
    ``json.dumps``/recursive Python object construction, so this helper can
    reach depths that would themselves blow either the interpreter's call
    stack or ``json.dumps``'s own internal recursion (measured ceiling on
    this manifest shape: ``json.loads`` raises ``RecursionError`` around
    depth 10000). ``__PLACEHOLDER__`` is a JSON string value in the base
    manifest so the substring replacement below only ever touches the one
    quoted placeholder, never a structural brace elsewhere in the document.
    """
    wraps = depth - 2
    nested = ('{"level":' * wraps) + '"leaf"' + ("}" * wraps)
    base = copy.deepcopy(_manifest())
    base["ui_state"] = {"nested": "__PLACEHOLDER__"}
    text = json.dumps(base, ensure_ascii=False, separators=(",", ":"))
    text = text.replace('"__PLACEHOLDER__"', nested)
    return text.encode("utf-8")


@pytest.mark.contract("C-A-082")
def test_load_rejects_moderate_over_limit_depth_before_recursive_walk(
    tmp_path: Path,
) -> None:
    """A depth well past the limit is rejected by the iterative check, not recursion.

    Depth 4000 is comfortably parseable by ``json.loads`` (measured ceiling
    ~9999 on this manifest shape) but far exceeds both ``MAX_GRAPH_DEPTH``
    (64) and Python's default recursion limit (1000). If
    ``validate_project_document`` calls the recursive
    ``_check_finite_recursive`` walk *before* the depth check, so a document
    at this depth raised a bare ``RecursionError`` from inside the domain
    validator -- and ``load_project`` only ever caught ``RecursionError``
    around ``json.loads``, not around ``validate_project_document`` -- so it
    propagate uncaught instead of becoming ``ProjectFormatError``.

    The ``~9999`` parser ceiling is an undocumented CPython implementation
    detail and may differ across versions or alternative implementations.
    Either way, this test does not depend on the exact ceiling value:
    ``load_project`` is defense-in-depth two layers deep -- the iterative,
    non-recursive ``_bounded_json_depth`` walk (which this depth-4000 case
    exercises) and a separate ``except RecursionError`` guard around
    ``json.loads`` itself (exercised by ``C-A-083`` below, for documents deep
    enough to blow the parser's own internal recursion) -- so both paths
    converge on ``ProjectFormatError`` regardless of where the numeric
    ceiling actually sits on a given interpreter.
    """
    path = tmp_path / "moderately-deep.gwxproj"
    path.write_bytes(_raw_deep_manifest_bytes(4000))

    try:
        load_project(path)
    except ProjectFormatError:
        return
    except RecursionError:
        raise AssertionError(
            "load_project leaked RecursionError instead of ProjectFormatError "
            "-- the iterative depth check must run before any recursive walk "
            "of the document"
        ) from None
    raise AssertionError("over-depth project document was accepted")


@pytest.mark.contract("C-A-083")
def test_load_converts_json_loads_recursion_error_into_format_error(
    tmp_path: Path,
) -> None:
    """A document deep enough to blow ``json.loads`` itself is still rejected cleanly.

    Depth 20000 is past the point where ``json.loads`` itself raises
    ``RecursionError`` while parsing (measured ceiling ~9999 on this
    manifest shape), while the serialized size stays far under
    ``PROJECT_SIZE_LIMIT_BYTES`` so the size gate cannot intercept it first.
    ``load_project`` must catch this ``RecursionError`` in addition to
    ``json.JSONDecodeError`` and convert it to ``ProjectFormatError``.

    The ``~9999`` parser ceiling is an undocumented CPython implementation
    detail and may differ across versions or alternative implementations.
    This test does not
    depend on the exact value: it only needs a depth past whatever that
    ceiling is on the running interpreter, and relies on ``load_project``'s
    second line of defense -- the ``except RecursionError`` guard around
    ``json.loads`` -- as the complement to the iterative
    ``_bounded_json_depth`` walk exercised by ``C-A-082`` above. Both paths
    converge on ``ProjectFormatError``.
    """
    from gwexpy_studio.persistence.project_io import PROJECT_SIZE_LIMIT_BYTES

    raw = _raw_deep_manifest_bytes(20000)
    assert len(raw) < PROJECT_SIZE_LIMIT_BYTES
    path = tmp_path / "far-too-deep.gwxproj"
    path.write_bytes(raw)

    try:
        load_project(path)
    except ProjectFormatError:
        return
    except RecursionError:
        raise AssertionError(
            "load_project leaked a raw RecursionError from json.loads instead "
            "of converting it to ProjectFormatError"
        ) from None
    raise AssertionError("pathologically deep project document was accepted")


def _graph_injection_manifest(operations: list[dict[str, Any]]) -> dict[str, Any]:
    """Build an otherwise-empty manifest carrying only the given operations.

    ``objects``/``executions``/``plots`` are left empty so the only thing
    that can reject the document is ``OperationGraph`` construction inside
    ``Project.from_dict``. The constructor must reject a cycle, forward
    reference, or duplicate ``op_id`` injected through external JSON.
    """
    document = copy.deepcopy(_manifest())
    document["objects"] = []
    document["operations"] = operations
    document["executions"] = []
    document["plots"] = []
    document["ui_state"] = {}
    return document


def _graph_op(
    op_id: str, *, inputs: dict[str, str] | None = None, outputs: list[str]
) -> dict[str, Any]:
    """Build one JSON-primitive operation record for graph-injection fixtures."""
    return {
        "op_id": op_id,
        "operation_id": "timeseries.detrend",
        "operation_schema": 1,
        "inputs": dict(inputs or {}),
        "params": {},
        "outputs": outputs,
    }


@pytest.mark.parametrize(
    "operations",
    [
        pytest.param(
            [
                _graph_op("op-2", inputs={"self": "obj-1"}, outputs=["obj-2"]),
                _graph_op("op-1", outputs=["obj-1"]),
            ],
            id="forward-reference",
            marks=pytest.mark.contract("C-A-078"),
        ),
        pytest.param(
            [
                _graph_op("op-1", inputs={"self": "obj-2"}, outputs=["obj-1"]),
                _graph_op("op-2", inputs={"self": "obj-1"}, outputs=["obj-2"]),
            ],
            id="cycle",
            marks=pytest.mark.contract("C-A-079"),
        ),
        pytest.param(
            [
                _graph_op("op-1", outputs=["obj-1"]),
                _graph_op("op-1", outputs=["obj-2"]),
            ],
            id="duplicate-op-id",
            marks=pytest.mark.contract("C-A-080"),
        ),
    ],
)
def test_load_rejects_graph_invariant_violations_from_external_json(
    tmp_path: Path, operations: list[dict[str, Any]]
) -> None:
    """``load_project`` rejects a cycle/forward-reference/duplicate op_id.

    Unlike C-A-013..016 (incremental ``OperationGraph.add()``) and
    C-A-075..077 (bulk ``OperationGraph(operations)`` in-process), this
    exercises the full external-JSON boundary: bytes on disk -> ``json.loads``
    -> ``Project.from_dict`` -> ``OperationGraph.__init__``. ``Project.validate()``
    alone cannot catch these -- it checks input/output references by set
    membership only, without regard to order or producibility, so a genuine
    two-operation cycle or a forward reference would pass it silently.
    ``OperationGraph.add()`` remains the single validation entry point.
    """
    document = _graph_injection_manifest(operations)
    path = tmp_path / "invalid-graph.gwxproj"
    _write_manifest(path, document)

    _expect_project_format_error(lambda: load_project(path))


@pytest.mark.contract("C-A-066")
def test_load_does_not_execute_workers_scripts_or_read_source_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Loading is data-only across source, worker, process, and script seams."""
    source = tmp_path / "source.h5"
    source.write_bytes(b"deterministic source")
    assert source.is_file()
    marker = tmp_path / "executed.txt"
    adjacent_script = tmp_path / "adjacent.py"
    adjacent_script.write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('executed')\n",
        encoding="utf-8",
    )
    document = _manifest()
    document["sources"][0]["uri"] = str(source)
    project_path = tmp_path / "project.gwxproj"
    _write_manifest(project_path, document)

    source_name = os.fspath(source)
    project_name = os.fspath(project_path)
    project_reads: list[str] = []

    def is_path(value: Any, expected: str) -> bool:
        try:
            return os.fspath(value) == expected
        except TypeError:
            return False

    def reject_non_project_path(value: Any, action: str) -> None:
        try:
            path_value = os.fspath(value)
        except TypeError:
            return
        if path_value != project_name:
            raise AssertionError(f"load {action} non-project path: {path_value}")

    def guarded_open(file: Any, *args: Any, **kwargs: Any) -> Any:
        if is_path(file, source_name):
            raise AssertionError("load opened the source URI")
        reject_non_project_path(file, "opened")
        if is_path(file, project_name):
            project_reads.append("builtins.open")
        return original_builtin_open(file, *args, **kwargs)

    def guarded_io_open(file: Any, *args: Any, **kwargs: Any) -> Any:
        if is_path(file, source_name):
            raise AssertionError("load opened the source URI")
        reject_non_project_path(file, "opened")
        if is_path(file, project_name):
            project_reads.append("io.open")
        return original_io_open(file, *args, **kwargs)

    def guarded_os_open(file: Any, *args: Any, **kwargs: Any) -> Any:
        if is_path(file, source_name):
            raise AssertionError("load opened the source URI")
        reject_non_project_path(file, "opened")
        if is_path(file, project_name):
            project_reads.append("os.open")
        return original_os_open(file, *args, **kwargs)

    def guarded_stat(file: Any, *args: Any, **kwargs: Any) -> os.stat_result:
        if is_path(file, source_name):
            raise AssertionError("load statted the source URI")
        reject_non_project_path(file, "statted")
        return original_os_stat(file, *args, **kwargs)

    def guarded_path_stat(self: Path, *args: Any, **kwargs: Any) -> os.stat_result:
        if is_path(self, source_name):
            raise AssertionError("load statted the source URI")
        reject_non_project_path(self, "statted")
        return original_path_stat(self, *args, **kwargs)

    def guarded_read_bytes(self: Path, *args: Any, **kwargs: Any) -> bytes:
        if is_path(self, source_name):
            raise AssertionError("load read the source URI")
        reject_non_project_path(self, "read")
        if is_path(self, project_name):
            project_reads.append("Path.read_bytes")
        return original_read_bytes(self, *args, **kwargs)

    def guarded_read_text(self: Path, *args: Any, **kwargs: Any) -> str:
        if is_path(self, source_name):
            raise AssertionError("load read the source URI")
        reject_non_project_path(self, "read")
        if is_path(self, project_name):
            project_reads.append("Path.read_text")
        return original_read_text(self, *args, **kwargs)

    def guarded_path_open(self: Path, *args: Any, **kwargs: Any) -> Any:
        if is_path(self, source_name):
            raise AssertionError("load opened the source URI")
        reject_non_project_path(self, "opened")
        if is_path(self, project_name):
            project_reads.append("Path.open")
        return original_path_open(self, *args, **kwargs)

    def forbidden_import(name: str, *args: Any, **kwargs: Any) -> Any:
        forbidden = (
            name == "subprocess"
            or name == "multiprocessing"
            or name.startswith("subprocess.")
            or name.startswith("multiprocessing.")
            or name in {"gwexpy", "gwpy", "numpy", "scipy"}
            or name.startswith("gwexpy_studio.worker")
            or name.startswith("gwexpy_studio.runtime")
        )
        if forbidden:
            raise AssertionError(f"load imported forbidden module: {name}")
        return original_import(name, *args, **kwargs)

    def forbidden_call(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("load crossed an execution seam")

    original_builtin_open = builtins.open
    original_io_open = io.open
    original_os_open = os.open
    original_os_stat = os.stat
    original_path_open = Path.open
    original_path_stat = Path.stat
    original_read_bytes = Path.read_bytes
    original_read_text = Path.read_text
    original_import = builtins.__import__

    monkeypatch.setattr(builtins, "open", guarded_open)
    monkeypatch.setattr(io, "open", guarded_io_open)
    monkeypatch.setattr(os, "open", guarded_os_open)
    monkeypatch.setattr(os, "stat", guarded_stat)
    monkeypatch.setattr(Path, "open", guarded_path_open)
    monkeypatch.setattr(Path, "stat", guarded_path_stat)
    monkeypatch.setattr(Path, "read_bytes", guarded_read_bytes)
    monkeypatch.setattr(Path, "read_text", guarded_read_text)
    monkeypatch.setattr(builtins, "__import__", forbidden_import)
    monkeypatch.setattr(WorkerClient, "start", forbidden_call)
    monkeypatch.setattr(StudioSession, "replay", forbidden_call)
    for name in ("run", "Popen", "call", "check_call", "check_output"):
        monkeypatch.setattr(subprocess, name, forbidden_call)
    monkeypatch.setattr(multiprocessing, "Process", forbidden_call)
    monkeypatch.setattr(multiprocessing, "get_context", forbidden_call)

    try:
        loaded = load_project(project_path)
    finally:
        monkeypatch.undo()

    assert loaded.project_id == "project-io"
    assert project_reads
    assert not marker.exists()
    assert adjacent_script.exists()


@pytest.mark.contract("C-A-067")
def test_load_preserves_stored_source_mtime_without_touching_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Load preserves the stored snapshot without inspecting the source URI."""
    source = tmp_path / "source.h5"
    source.write_bytes(b"deterministic source")
    document = _manifest()
    document["sources"][0]["uri"] = str(source)
    document["sources"][0]["mtime"] = 123.0
    path = tmp_path / "project.gwxproj"
    _write_manifest(path, document)

    source_name = os.fspath(source)
    original_path_stat = Path.stat

    def guarded_path_stat(self: Path, *args: Any, **kwargs: Any) -> os.stat_result:
        if os.fspath(self) == source_name:
            raise AssertionError("load statted the source URI")
        return original_path_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", guarded_path_stat)
    try:
        loaded = load_project(path)
    finally:
        monkeypatch.undo()

    assert loaded.project_id == "project-io"
    assert loaded.sources[0].mtime == 123.0


@pytest.mark.contract("C-A-068")
def test_atomic_save_failure_preserves_old_file_modified_and_temp_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed replacement leaves both the old bytes and modified timestamp intact."""
    path = tmp_path / "project.gwxproj"
    old_bytes = b"old deterministic project"
    path.write_bytes(old_bytes)
    project = Project(
        project_id="project-io",
        created="2026-08-16T00:00:00Z",
        modified="old-modified",
        compatibility={"studio": "0.1.0", "gwexpy": "0.1.14"},
    )

    def fail_replace(*_args: Any, **_kwargs: Any) -> None:
        raise OSError("deterministic replace failure")

    monkeypatch.setattr(os, "replace", fail_replace)

    try:
        save_project(project, path, clock=lambda: "new-modified")
    except OSError as error:
        assert str(error) == "deterministic replace failure"
    else:
        raise AssertionError("atomic replacement unexpectedly succeeded")

    assert path.read_bytes() == old_bytes
    assert project.modified == "old-modified"
    assert tuple(item.name for item in tmp_path.iterdir()) == (path.name,)


@pytest.mark.contract("C-A-069")
def test_successful_save_updates_modified_only_from_injected_clock(
    tmp_path: Path,
) -> None:
    """Successful save uses the injected clock and persists its value."""
    path = tmp_path / "project.gwxproj"
    project = Project(
        project_id="project-io",
        created="2026-08-16T00:00:00Z",
        modified="old-modified",
        compatibility={"studio": "0.1.0", "gwexpy": "0.1.14"},
    )

    save_project(project, path, clock=lambda: "injected-modified")

    assert project.modified == "injected-modified"
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["modified"] == "injected-modified"
