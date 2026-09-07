"""Contract tests for shallow, explicit source inspection."""

from __future__ import annotations

import ast
import builtins
import inspect
import io
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest

from gwexpy_studio.errors import PrototypeNotImplementedError
from gwexpy_studio.ops import source as source_module
from gwexpy_studio.ops.source import SourceInspection, inspect_source

HDF5_MAGIC = b"\x89HDF\r\n\x1a\n"
MAX_INSPECTION_READ_BYTES = 4096
SOURCE_MODULE_PATH = Path(inspect.getfile(source_module)).resolve()
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOT = SOURCE_MODULE_PATH.parents[2]


def _mismatched_hdf5_magic(index: int) -> bytes:
    """Flip exactly one selected byte in the eight-byte HDF5 signature."""
    assert 0 <= index < len(HDF5_MAGIC)
    replacement = bytes([HDF5_MAGIC[index] ^ 1])
    return HDF5_MAGIC[:index] + replacement + HDF5_MAGIC[index + 1 :]


class _ForbiddenSourceAccess(AssertionError):
    """Raised when source inspection accesses a non-source or non-regular path."""


class _ReadBudget:
    """Track requested and returned bytes across every permitted read API."""

    def __init__(self) -> None:
        self.events: list[tuple[str, int, int]] = []
        self.requested = 0
        self.actual = 0

    def record(self, api: str, requested: int, actual: int) -> None:
        assert type(requested) is int
        assert 0 < requested <= MAX_INSPECTION_READ_BYTES
        assert type(actual) is int
        assert 0 <= actual <= requested
        self.requested += requested
        self.actual += actual
        assert self.requested <= MAX_INSPECTION_READ_BYTES
        assert self.actual <= MAX_INSPECTION_READ_BYTES
        self.events.append((api, requested, actual))


def _inspect_file(path: Path, format_guess: Any) -> None:
    """Compare every declared field with one explicit filesystem snapshot."""
    stat_result = path.stat()
    actual = inspect_source(str(path))

    assert type(actual) is SourceInspection
    assert actual == SourceInspection(
        exists=True,
        size_bytes=stat_result.st_size,
        mtime=stat_result.st_mtime,
        format_guess=format_guess,
        resolved_uri=str(path.resolve()),
        device=stat_result.st_dev,
        inode=stat_result.st_ino,
        mtime_ns=stat_result.st_mtime_ns,
    )


class _BoundedReader:
    """Proxy that permits only explicitly bounded file-object reads."""

    def __init__(self, wrapped: Any, budget: _ReadBudget, target_fds: set[int]) -> None:
        self._wrapped = wrapped
        self._budget = budget
        self._target_fds = target_fds
        try:
            self._target_fds.add(wrapped.fileno())
        except (AttributeError, OSError, ValueError):
            pass

    def read(self, size: int = -1) -> Any:
        """Require an explicit bounded size and record returned bytes."""
        assert type(size) is int
        assert 0 < size <= MAX_INSPECTION_READ_BYTES
        result = self._wrapped.read(size)
        self._budget.record("read", size, len(result))
        return result

    def readinto(self, buffer: Any) -> int:
        """Treat the supplied buffer length as the explicit read bound."""
        requested = memoryview(buffer).nbytes
        assert 0 < requested <= MAX_INSPECTION_READ_BYTES
        result = self._wrapped.readinto(buffer)
        self._budget.record("readinto", requested, result)
        return result

    def read1(self, size: int = -1) -> Any:
        """Require an explicit bounded size for buffered read1 calls."""
        assert type(size) is int
        assert 0 < size <= MAX_INSPECTION_READ_BYTES
        result = self._wrapped.read1(size)
        self._budget.record("read1", size, len(result))
        return result

    def __enter__(self) -> _BoundedReader:
        self._wrapped.__enter__()
        return self

    def __exit__(self, *args: Any) -> Any:
        return self._wrapped.__exit__(*args)

    def __getattr__(self, name: str) -> Any:
        if name in {
            "readall",
            "readinto1",
            "readline",
            "readlines",
        }:
            raise AssertionError(f"unbounded read method used: {name}")
        return getattr(self._wrapped, name)


def _is_target_path(candidate: Any, target: Path) -> bool:
    """Identify only the inspected path without constraining opener choice."""
    try:
        return os.fspath(candidate) == os.fspath(target)
    except TypeError:
        return False


def _bounded_opener(
    original_open: Any,
    target: Path,
    budget: _ReadBudget,
    target_fds: set[int],
) -> Any:
    """Wrap one common opener while preserving its public call shape."""

    def opener(file: Any, *args: Any, **kwargs: Any) -> Any:
        wrapped = original_open(file, *args, **kwargs)
        if _is_target_path(file, target) or (type(file) is int and file in target_fds):
            return _BoundedReader(wrapped, budget, target_fds)
        return wrapped

    return opener


def _module_tokens(value: str) -> set[str]:
    """Normalize dotted import paths for boundary assertions."""
    return {part.strip("_").lower() for part in value.split(".") if part}


def _assert_shallow_source_module_implementation() -> None:
    """Reject forbidden imports, aliases, readers, and unbounded read syntax."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(source_module)))
    forbidden_import_tokens = {
        "gwexpy",
        "gwpy",
        "multiprocessing",
        "subprocess",
        "importlib",
        "runpy",
        "mmap",
        "executor",
        "worker",
        "replay",
        "registry",
        "timeseries",
        "skeleton",
    }
    forbidden_names = {
        "REGISTRY",
        "TimeSeries",
        "gwexpy",
        "gwpy",
        "__import__",
        "eval",
        "exec",
        "compile",
        "run_path",
        "import_module",
        "run_module",
        "defer",
    }
    forbidden_methods = {
        "REGISTRY",
        "TimeSeries",
        "readv",
        "peek",
        "read_bytes",
        "read_text",
        "readall",
        "readinto1",
        "readline",
        "readlines",
        "read_full",
        "preadv",
        "preadv2",
        "recv",
        "recv_into",
        "import_module",
        "run_module",
        "Popen",
        "system",
        "popen",
        "defer",
    }
    bounded_methods = {"read", "readinto", "read1", "pread"}

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not forbidden_import_tokens.intersection(
                    _module_tokens(alias.name)
                )
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            assert not forbidden_import_tokens.intersection(_module_tokens(module))
            for alias in node.names:
                # `from . import skeleton` / `from .. import skeleton` puts
                # the forbidden module name in alias.name, not node.module
                # (node.module is None for a bare relative import). Treat
                # alias.name as a module token too, alongside the existing
                # forbidden-name check.
                assert not forbidden_import_tokens.intersection(
                    _module_tokens(alias.name)
                )
                assert alias.name not in forbidden_names
        elif isinstance(node, ast.Name):
            assert node.id not in forbidden_names
        elif isinstance(node, ast.Attribute):
            assert node.attr not in forbidden_methods
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in bounded_methods:
                assert node.args or node.keywords


def _run_fresh_source_boundary_tripwire(
    uri: Path, mode: str = "inspect"
) -> subprocess.CompletedProcess[str]:
    """Call source inspection under import, execution, and read tripwires."""
    child = textwrap.dedent(
        """
        import builtins
        import io
        import os
        import sys
        import types
        from pathlib import Path

        target_path = sys.argv[1]
        mode = sys.argv[2]
        target_fds = set()
        read_events = []
        requested_total = 0
        actual_total = 0

        def is_target(value):
            try:
                return os.fspath(value) == target_path
            except TypeError:
                return False

        def record(api, requested, actual):
            global requested_total, actual_total
            if type(requested) is not int or not 0 < requested <= 4096:
                raise AssertionError(f"unbounded {api} request: {requested!r}")
            if type(actual) is not int or not 0 <= actual <= requested:
                raise AssertionError(f"invalid {api} result: {actual!r}")
            requested_total += requested
            actual_total += actual
            if requested_total > 4096 or actual_total > 4096:
                raise AssertionError("aggregate source read budget exceeded")
            read_events.append(api)

        class BoundedReader:
            def __init__(self, wrapped):
                self._wrapped = wrapped
                try:
                    target_fds.add(wrapped.fileno())
                except (AttributeError, OSError, ValueError):
                    pass

            def read(self, size=-1):
                if type(size) is not int or not 0 < size <= 4096:
                    raise AssertionError(f"unbounded read request: {size!r}")
                result = self._wrapped.read(size)
                record("read", size, len(result))
                return result

            def readinto(self, buffer):
                requested = memoryview(buffer).nbytes
                if not 0 < requested <= 4096:
                    raise AssertionError("unbounded readinto request")
                result = self._wrapped.readinto(buffer)
                record("readinto", requested, result)
                return result

            def read1(self, size=-1):
                if type(size) is not int or not 0 < size <= 4096:
                    raise AssertionError(f"unbounded read1 request: {size!r}")
                result = self._wrapped.read1(size)
                record("read1", size, len(result))
                return result

            def __enter__(self):
                self._wrapped.__enter__()
                return self

            def __exit__(self, *args):
                return self._wrapped.__exit__(*args)

            def __getattr__(self, name):
                if name in {
                    "readall",
                    "readinto1",
                    "readline",
                    "readlines",
                }:
                    raise AssertionError(f"forbidden full read method: {name}")
                return getattr(self._wrapped, name)

        def wrap_stream(stream, source):
            if is_target(source) or (
                isinstance(source, int) and source in target_fds
            ):
                return BoundedReader(stream)
            return stream

        real_open = builtins.open
        real_io_open = io.open
        real_os_open = os.open
        real_os_fdopen = os.fdopen
        real_file_io = io.FileIO
        real_os_read = os.read
        real_os_pread = getattr(os, "pread", None)

        def bounded_open(source, *args, **kwargs):
            return wrap_stream(real_open(source, *args, **kwargs), source)

        def bounded_io_open(source, *args, **kwargs):
            return wrap_stream(real_io_open(source, *args, **kwargs), source)

        def bounded_os_open(*args, **kwargs):
            source = args[0] if args else kwargs.get("path")
            descriptor = real_os_open(*args, **kwargs)
            if is_target(source):
                target_fds.add(descriptor)
            return descriptor

        def bounded_fdopen(descriptor, *args, **kwargs):
            return wrap_stream(
                real_os_fdopen(descriptor, *args, **kwargs), descriptor
            )

        def bounded_file_io(source, *args, **kwargs):
            return wrap_stream(real_file_io(source, *args, **kwargs), source)

        def bounded_os_read(descriptor, size):
            if descriptor not in target_fds:
                return real_os_read(descriptor, size)
            if type(size) is not int or not 0 < size <= 4096:
                raise AssertionError(f"unbounded os.read request: {size!r}")
            result = real_os_read(descriptor, size)
            record("os.read", size, len(result))
            return result

        def bounded_os_pread(descriptor, size, offset):
            if descriptor not in target_fds:
                return real_os_pread(descriptor, size, offset)
            if type(size) is not int or not 0 < size <= 4096:
                raise AssertionError(f"unbounded os.pread request: {size!r}")
            result = real_os_pread(descriptor, size, offset)
            record("os.pread", size, len(result))
            return result

        def forbidden_access(*args, **kwargs):
            raise AssertionError("FIFO open/read attempted")

        if mode == "fifo":
            builtins.open = forbidden_access
            io.open = forbidden_access
            io.FileIO = forbidden_access
            os.open = forbidden_access
            os.fdopen = forbidden_access
            os.read = forbidden_access
            if real_os_pread is not None:
                os.pread = forbidden_access
        else:
            builtins.open = bounded_open
            io.open = bounded_io_open
            io.FileIO = bounded_file_io
            os.open = bounded_os_open
            os.fdopen = bounded_fdopen
            os.read = bounded_os_read
            if real_os_pread is not None:
                os.pread = bounded_os_pread

        def forbidden_path_read(*args, **kwargs):
            raise AssertionError("Path.read_bytes/read_text is forbidden")

        Path.read_bytes = forbidden_path_read
        Path.read_text = forbidden_path_read

        source_root = os.environ["GWEXPY_STUDIO_SOURCE_ROOT"]
        studio_package = types.ModuleType("gwexpy_studio")
        studio_package.__path__ = [os.path.join(source_root, "gwexpy_studio")]
        sys.modules["gwexpy_studio"] = studio_package
        ops_package = types.ModuleType("gwexpy_studio.ops")
        ops_package.__path__ = [
            os.path.join(source_root, "gwexpy_studio", "ops")
        ]
        sys.modules["gwexpy_studio.ops"] = ops_package

        real_import = builtins.__import__
        blocked_tokens = {
            "gwexpy",
            "gwpy",
            "multiprocessing",
            "subprocess",
            "executor",
            "worker",
            "replay",
            "registry",
            "timeseries",
        }

        def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
            tokens = {part.strip("_").lower() for part in name.split(".") if part}
            if blocked_tokens.intersection(tokens):
                raise AssertionError(f"forbidden source import: {name}")
            if {str(item) for item in (fromlist or ())}.intersection(
                {"REGISTRY", "TimeSeries"}
            ):
                raise AssertionError(f"forbidden source reader alias: {fromlist}")
            return real_import(name, globals, locals, fromlist, level)

        builtins.__import__ = guarded_import

        def forbidden_execution(*args, **kwargs):
            raise AssertionError("forbidden external execution")

        for name in (
            "system",
            "popen",
            "execv",
            "execve",
            "execl",
            "execlp",
            "execle",
            "execvp",
            "execvpe",
            "spawnl",
            "spawnle",
            "spawnlp",
            "spawnlpe",
            "spawnv",
            "spawnve",
            "spawnvp",
            "spawnvpe",
        ):
            if hasattr(os, name):
                setattr(os, name, forbidden_execution)

        from gwexpy_studio.errors import PrototypeNotImplementedError
        from gwexpy_studio.ops.source import SourceInspection, inspect_source

        try:
            result = inspect_source(sys.argv[1])
        except PrototypeNotImplementedError as error:
            if (
                type(error) is PrototypeNotImplementedError
                and error.code == "STUDIO-FOUNDATION-NOT-IMPLEMENTED"
                and error.owner == "gwexpy_studio.ops.source.inspect_source"
            ):
                print(
                    "fifo-sentinel" if mode == "fifo" else "source-boundary-sentinel"
                )
            else:
                raise
        except (OSError, ValueError):
            if mode == "fifo":
                print("fifo-rejected")
            else:
                raise
        else:
            if mode == "fifo":
                raise AssertionError(f"FIFO was accepted: {result!r}")
            if type(result) is not SourceInspection:
                raise AssertionError("inspect_source returned the wrong type")
            api_names = ",".join(sorted(set(read_events))) or "-"
            print(
                f"source-boundary-ok:{result.format_guess or 'none'}:"
                f"{requested_total}:{actual_total}:{api_names}"
            )
        """
    )
    env = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "GWEXPY_STUDIO_SOURCE_ROOT": str(SOURCE_ROOT),
        "PYTHONPATH": os.pathsep.join((str(SOURCE_ROOT), str(REPOSITORY_ROOT))),
    }
    return subprocess.run(
        [sys.executable, "-S", "-c", child, str(uri), mode],
        cwd=REPOSITORY_ROOT,
        env=env,
        capture_output=True,
        check=False,
        text=True,
        timeout=5 if mode == "fifo" else 10,
    )


@pytest.mark.contract("B-063")
def test_source_module_boundary_is_studio_and_reader_free(
    tmp_path: Path,
) -> None:
    """The whole source module stays outside readers and execution paths."""
    _assert_shallow_source_module_implementation()
    source = tmp_path / "boundary.bin"
    source.write_bytes(b"arbitrary source bytes")

    child = _run_fresh_source_boundary_tripwire(source)
    assert child.returncode == 0, (
        f"fresh source boundary failed: stdout={child.stdout!r} stderr={child.stderr!r}"
    )
    outcome = child.stdout.strip()
    assert outcome == "source-boundary-sentinel" or outcome.startswith(
        "source-boundary-ok:"
    )

    try:
        actual = inspect_source(str(source))
    except PrototypeNotImplementedError as error:
        assert type(error) is PrototypeNotImplementedError
        assert error.code == "STUDIO-FOUNDATION-NOT-IMPLEMENTED"
        assert error.owner == "gwexpy_studio.ops.source.inspect_source"
        raise
    assert type(actual) is SourceInspection


def _assert_fresh_branch_result(
    child: subprocess.CompletedProcess[str], expected_format: str | None
) -> bool:
    """Validate one fresh-process branch result and report sentinel state."""
    assert child.returncode == 0, (
        f"fresh source branch failed: stdout={child.stdout!r} stderr={child.stderr!r}"
    )
    outcome = child.stdout.strip()
    if outcome == "source-boundary-sentinel":
        return False

    parts = outcome.split(":", 4)
    assert len(parts) == 5
    assert parts[0] == "source-boundary-ok"
    assert parts[1] == (expected_format or "none")
    requested = int(parts[2])
    actual = int(parts[3])
    assert 0 <= requested <= MAX_INSPECTION_READ_BYTES
    assert 0 <= actual <= MAX_INSPECTION_READ_BYTES
    assert parts[4] == "-" or set(parts[4].split(",")).issubset(
        {"read", "readinto", "read1", "os.read", "os.pread"}
    )
    return True


@pytest.mark.parametrize(
    "source_kind",
    [
        pytest.param(
            "extension-hdf5",
            id="extension-hdf5",
            marks=pytest.mark.contract("B-076"),
        ),
        pytest.param(
            "extension-csv",
            id="extension-csv",
            marks=pytest.mark.contract("B-077"),
        ),
        pytest.param(
            "magic-bin",
            id="magic-bin",
            marks=pytest.mark.contract("B-078"),
        ),
    ],
)
def test_fresh_branch_tripwire_covers_extension_and_magic_decisions(
    source_kind: str, hdf5_source: Path, tmp_path: Path
) -> None:
    """Each source decision branch shares the reader-free bounded seam."""
    if source_kind == "extension-hdf5":
        source = hdf5_source
        expected_format = "hdf5"
    elif source_kind == "extension-csv":
        source = tmp_path / "branch.csv"
        source.write_text("time,value\n0,0\n1,1\n", encoding="utf-8")
        expected_format = "csv_enhanced"
    else:
        source = tmp_path / "branch.bin"
        source.write_bytes(HDF5_MAGIC + b"magic-only branch payload")
        expected_format = "hdf5"

    child = _run_fresh_source_boundary_tripwire(source)
    if not _assert_fresh_branch_result(child, expected_format):
        try:
            inspect_source(str(source))
        except PrototypeNotImplementedError as error:
            assert type(error) is PrototypeNotImplementedError
            assert error.code == "STUDIO-FOUNDATION-NOT-IMPLEMENTED"
            assert error.owner == "gwexpy_studio.ops.source.inspect_source"
            raise
        raise AssertionError("inspect_source no longer exposes the foundation sentinel")

    actual = inspect_source(str(source))
    assert type(actual) is SourceInspection
    assert actual.format_guess == expected_format


@pytest.mark.parametrize(
    "source_kind",
    [
        pytest.param(
            "hdf5",
            id="named-hdf5",
            marks=pytest.mark.contract("B-056"),
        ),
        pytest.param(
            "csv_enhanced",
            id="csv-enhanced",
            marks=pytest.mark.contract("B-057"),
        ),
    ],
)
def test_inspect_source_returns_exact_regular_file_metadata(
    source_kind: str, hdf5_source: Path, tmp_path: Path
) -> None:
    """Named HDF5 and csv_enhanced files expose exact shallow metadata."""
    if source_kind == "hdf5":
        source = hdf5_source
    else:
        source = tmp_path / "named.csv"
        source.write_text("time,value\n0,0\n1,1\n", encoding="utf-8")
        fixed_ns = 1_700_000_000_123_456_789
        os.utime(source, ns=(fixed_ns, fixed_ns))

    _inspect_file(source, source_kind)


@pytest.mark.contract("B-058")
def test_inspect_source_uses_only_bounded_extension_and_magic_reads(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Inspection permits only bounded shallow reads capped at 4096 bytes."""
    _assert_shallow_source_module_implementation()
    source = tmp_path / "unknown-extension.bin"
    source.write_bytes(HDF5_MAGIC + b"untrusted payload\n" * 32)
    budget = _ReadBudget()
    target_fds: set[int] = set()

    def forbidden_path_read(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Path.read_bytes/read_text is forbidden")

    monkeypatch.setattr(Path, "read_bytes", forbidden_path_read)
    monkeypatch.setattr(Path, "read_text", forbidden_path_read)
    monkeypatch.setattr(
        builtins,
        "open",
        _bounded_opener(builtins.open, source, budget, target_fds),
    )
    monkeypatch.setattr(
        io,
        "open",
        _bounded_opener(io.open, source, budget, target_fds),
    )

    original_os_fdopen = os.fdopen

    def bounded_os_fdopen(file_descriptor: int, *args: Any, **kwargs: Any) -> Any:
        wrapped = original_os_fdopen(file_descriptor, *args, **kwargs)
        if file_descriptor in target_fds:
            return _BoundedReader(wrapped, budget, target_fds)
        return wrapped

    monkeypatch.setattr(os, "fdopen", bounded_os_fdopen)

    original_file_io = io.FileIO

    def bounded_file_io(file: Any, *args: Any, **kwargs: Any) -> Any:
        wrapped = original_file_io(file, *args, **kwargs)
        if _is_target_path(file, source) or (type(file) is int and file in target_fds):
            return _BoundedReader(wrapped, budget, target_fds)
        return wrapped

    monkeypatch.setattr(io, "FileIO", bounded_file_io)

    original_os_open = os.open

    def bounded_os_open(*args: Any, **kwargs: Any) -> int:
        file = args[0] if args else kwargs.get("path")
        file_descriptor = original_os_open(*args, **kwargs)
        if _is_target_path(file, source):
            target_fds.add(file_descriptor)
        return file_descriptor

    monkeypatch.setattr(os, "open", bounded_os_open)

    original_os_read = os.read

    def bounded_os_read(file_descriptor: int, size: int) -> bytes:
        if file_descriptor not in target_fds:
            return original_os_read(file_descriptor, size)
        assert type(size) is int
        assert 0 < size <= MAX_INSPECTION_READ_BYTES
        result = original_os_read(file_descriptor, size)
        budget.record("os.read", size, len(result))
        return result

    monkeypatch.setattr(os, "read", bounded_os_read)

    original_os_pread = getattr(os, "pread", None)
    if original_os_pread is not None:

        def bounded_os_pread(file_descriptor: int, size: int, offset: int) -> bytes:
            if file_descriptor not in target_fds:
                return original_os_pread(file_descriptor, size, offset)
            assert type(size) is int
            assert 0 < size <= MAX_INSPECTION_READ_BYTES
            result = original_os_pread(file_descriptor, size, offset)
            budget.record("os.pread", size, len(result))
            return result

        monkeypatch.setattr(os, "pread", bounded_os_pread)

    actual = inspect_source(str(source))

    stat_result = source.stat()
    assert actual == SourceInspection(
        exists=True,
        size_bytes=stat_result.st_size,
        mtime=stat_result.st_mtime,
        format_guess="hdf5",
        resolved_uri=str(source.resolve()),
        device=stat_result.st_dev,
        inode=stat_result.st_ino,
        mtime_ns=stat_result.st_mtime_ns,
    )
    assert budget.events
    assert {event[0] for event in budget.events} <= {
        "read",
        "readinto",
        "read1",
        "os.read",
        "os.pread",
    }
    assert budget.requested <= MAX_INSPECTION_READ_BYTES
    assert budget.actual <= MAX_INSPECTION_READ_BYTES


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(
            HDF5_MAGIC[:-1],
            id="truncated-hdf5-magic",
            marks=pytest.mark.contract("B-064"),
        ),
        pytest.param(
            _mismatched_hdf5_magic(0),
            id="one-byte-hdf5-mismatch",
            marks=pytest.mark.contract("B-065"),
        ),
        pytest.param(
            _mismatched_hdf5_magic(1),
            id="one-byte-hdf5-mismatch-byte-1",
            marks=pytest.mark.contract("B-069"),
        ),
        pytest.param(
            _mismatched_hdf5_magic(2),
            id="one-byte-hdf5-mismatch-byte-2",
            marks=pytest.mark.contract("B-070"),
        ),
        pytest.param(
            _mismatched_hdf5_magic(3),
            id="one-byte-hdf5-mismatch-byte-3",
            marks=pytest.mark.contract("B-071"),
        ),
        pytest.param(
            _mismatched_hdf5_magic(4),
            id="one-byte-hdf5-mismatch-byte-4",
            marks=pytest.mark.contract("B-072"),
        ),
        pytest.param(
            _mismatched_hdf5_magic(5),
            id="one-byte-hdf5-mismatch-byte-5",
            marks=pytest.mark.contract("B-073"),
        ),
        pytest.param(
            _mismatched_hdf5_magic(6),
            id="one-byte-hdf5-mismatch-byte-6",
            marks=pytest.mark.contract("B-074"),
        ),
        pytest.param(
            _mismatched_hdf5_magic(7),
            id="one-byte-hdf5-mismatch-byte-7",
            marks=pytest.mark.contract("B-075"),
        ),
        pytest.param(
            b"\x00" + HDF5_MAGIC,
            id="hdf5-magic-at-nonzero-offset",
            marks=pytest.mark.contract("B-066"),
        ),
        pytest.param(
            b"arbitrary binary payload",
            id="arbitrary-bin",
            marks=pytest.mark.contract("B-067"),
        ),
    ],
)
def test_inspect_source_rejects_non_exact_hdf5_magic_canaries(
    payload: bytes, tmp_path: Path
) -> None:
    """Only an exact HDF5 prefix can produce the HDF5 format guess."""
    source = tmp_path / "canary.bin"
    source.write_bytes(payload)

    child = _run_fresh_source_boundary_tripwire(source)
    if not _assert_fresh_branch_result(child, None):
        try:
            inspect_source(str(source))
        except PrototypeNotImplementedError as error:
            assert type(error) is PrototypeNotImplementedError
            assert error.code == "STUDIO-FOUNDATION-NOT-IMPLEMENTED"
            assert error.owner == "gwexpy_studio.ops.source.inspect_source"
            raise
        raise AssertionError("inspect_source no longer exposes the foundation sentinel")

    stat_result = source.stat()
    actual = inspect_source(str(source))

    assert type(actual) is SourceInspection
    assert actual == SourceInspection(
        exists=True,
        size_bytes=stat_result.st_size,
        mtime=stat_result.st_mtime,
        format_guess=None,
        resolved_uri=str(source.resolve()),
        device=stat_result.st_dev,
        inode=stat_result.st_ino,
        mtime_ns=stat_result.st_mtime_ns,
    )


@pytest.mark.contract("B-059")
def test_inspect_source_reports_missing_file_as_data_only_absence(
    tmp_path: Path,
) -> None:
    """A missing URI is data-only absence, not an executable source."""
    actual = inspect_source(str(tmp_path / "missing.h5"))

    assert actual == SourceInspection(
        exists=False,
        size_bytes=None,
        mtime=None,
        format_guess=None,
    )


@pytest.mark.contract("B-060")
def test_inspect_source_rejects_directory_as_non_regular_source(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A directory is rejected before any open or read attempt."""
    directory = tmp_path / "source.h5"
    directory.mkdir()
    accesses: list[str] = []

    def forbidden_access(*args: Any, **kwargs: Any) -> Any:
        accesses.append("open-or-read")
        raise _ForbiddenSourceAccess("directory access must be preclassified")

    monkeypatch.setattr(Path, "read_bytes", forbidden_access)
    monkeypatch.setattr(Path, "read_text", forbidden_access)
    monkeypatch.setattr(builtins, "open", forbidden_access)
    monkeypatch.setattr(io, "open", forbidden_access)
    monkeypatch.setattr(os, "open", forbidden_access)
    monkeypatch.setattr(os, "read", forbidden_access)
    if hasattr(os, "pread"):
        monkeypatch.setattr(os, "pread", forbidden_access)

    with pytest.raises(IsADirectoryError) as raised:
        inspect_source(str(directory))

    assert type(raised.value) is IsADirectoryError
    assert accesses == []


@pytest.mark.contract("B-068")
def test_inspect_source_rejects_linux_fifo_without_blocking(
    tmp_path: Path,
) -> None:
    """A Linux FIFO is rejected without opening or blocking on the pipe."""
    assert sys.platform.startswith("linux")
    fifo = tmp_path / "source.fifo"
    os.mkfifo(fifo)

    child = _run_fresh_source_boundary_tripwire(fifo, mode="fifo")
    assert child.returncode == 0, (
        f"fresh FIFO rejection failed: stdout={child.stdout!r} stderr={child.stderr!r}"
    )
    outcome = child.stdout.strip()
    assert outcome in {"fifo-sentinel", "fifo-rejected"}
    if outcome == "fifo-rejected":
        return

    try:
        inspect_source(str(fifo))
    except PrototypeNotImplementedError as error:
        assert type(error) is PrototypeNotImplementedError
        assert error.code == "STUDIO-FOUNDATION-NOT-IMPLEMENTED"
        assert error.owner == "gwexpy_studio.ops.source.inspect_source"
        raise
    raise AssertionError("inspect_source no longer exposes the foundation sentinel")


@pytest.mark.contract("B-061")
def test_inspect_source_observes_mtime_only_on_second_explicit_call(
    tmp_path: Path,
) -> None:
    """A changed mtime appears only in a later explicit inspection result."""
    source = tmp_path / "mtime.h5"
    source.write_bytes(HDF5_MAGIC)
    first_stat = source.stat()
    first = inspect_source(str(source))
    assert first == SourceInspection(
        exists=True,
        size_bytes=first_stat.st_size,
        mtime=first_stat.st_mtime,
        format_guess="hdf5",
        resolved_uri=str(source.resolve()),
        device=first_stat.st_dev,
        inode=first_stat.st_ino,
        mtime_ns=first_stat.st_mtime_ns,
    )

    changed_ns = first_stat.st_mtime_ns + 2_000_000_000
    os.utime(source, ns=(changed_ns, changed_ns))
    second = inspect_source(str(source))
    second_stat = source.stat()

    assert second == SourceInspection(
        exists=True,
        size_bytes=second_stat.st_size,
        mtime=second_stat.st_mtime,
        format_guess="hdf5",
        resolved_uri=str(source.resolve()),
        device=second_stat.st_dev,
        inode=second_stat.st_ino,
        mtime_ns=second_stat.st_mtime_ns,
    )
    assert second.mtime != first.mtime


@pytest.mark.contract("B-062")
def test_format_guess_does_not_make_timeseries_read_format_optional(
    tmp_path: Path,
) -> None:
    """Inspection hints never replace explicit timeseries.read format."""
    source = tmp_path / "source.h5"
    source.write_bytes(HDF5_MAGIC)
    actual = inspect_source(str(source))

    from gwexpy_studio.ops.timeseries import REGISTRY

    read_params = {param.name: param for param in REGISTRY["timeseries.read"].params}
    format_param = read_params["format"]
    assert actual.format_guess == "hdf5"
    assert format_param.required is True
    assert format_param.default is None
    assert format_param.choices == ("hdf5", "csv_enhanced")
