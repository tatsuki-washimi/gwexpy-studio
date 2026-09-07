"""Qt-free alpha application controller implementation."""

from __future__ import annotations

import multiprocessing
import os
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import numpy as np

from ..domain.graph import OperationGraph
from ..domain.model import DataObjectRef, DataSourceRef, Operation, PlotSpec
from ..domain.project import Project
from ..errors import OperationError
from ..export.python_exporter import export_python
from ..ops.source import SourceInspection, inspect_source
from ..ops.spec import normalize_params
from ..ops.timeseries import REGISTRY
from ..session import StudioSession
from ..worker.client import WorkerClient
from ..worker.service import worker_main
from .preview import PreviewData, PreviewPayload
from .project_factory import create_alpha_project
from .signal_controller import SignalController

MAX_SOURCE_BYTES = 512 * 1024 * 1024  # 512 MiB
FORBIDDEN_SCRIPT_SUFFIXES = frozenset(
    {".py", ".sh", ".bash", ".zsh", ".c", ".cpp", ".rs", ".js", ".ts", ".exe", ".bin"}
)


def _add_preview_failure_note(
    primary: BaseException, label: str, failure: BaseException
) -> None:
    """Attach one secondary failure note without letting diagnostics escape."""
    try:
        type_name = type(failure).__name__
        if not isinstance(type_name, str) or not type_name:
            type_name = "BaseException"
    except BaseException:
        type_name = "BaseException"
    try:
        message = str(failure)
    except BaseException:
        message = ""
    try:
        detail = f"{type_name}: {message}" if message else type_name
        note = f"{label}: {detail}"
    except BaseException:
        return
    try:
        if note in getattr(primary, "__notes__", ()):
            return
    except BaseException:
        pass
    try:
        primary.add_note(note)
    except BaseException:
        pass


class AlphaController(SignalController):
    """Application controller coordinating project mutations and headless execution."""

    def __init__(
        self,
        *,
        project: Project | None = None,
        session: StudioSession | None = None,
        client: WorkerClient | None = None,
    ) -> None:
        """Initialize controller with project and execution session."""
        if project is None:
            self.project = create_alpha_project()
        else:
            self.project = project

        if session is None:
            actual_client = client
            if actual_client is None:
                ctx = multiprocessing.get_context("spawn")
                actual_client = WorkerClient(
                    context=ctx,
                    process_factory=ctx.Process,
                    connection_factory=ctx.Pipe,
                    worker_target=worker_main,
                    startup_timeout_s=60.0,
                    request_timeout_s=90.0,
                    join_timeout_s=15.0,
                )
            self.session = StudioSession(
                project=self.project,
                graph=self.project.graph,
                client=actual_client,
            )
        else:
            self.session = session
            if getattr(self.session, "project", None) is None:
                try:
                    self.session.project = self.project
                except AttributeError:
                    pass
            if getattr(self.session, "graph", None) is None:
                try:
                    self.session.graph = self.project.graph
                except AttributeError:
                    pass

    def _ensure_session_started(self) -> None:
        """Ensure the session worker is running before executing operations."""
        if self.session is not None:
            client = getattr(self.session, "client", None)
            if client is not None:
                state = getattr(client, "state", None)
                state_val = getattr(state, "value", str(state))
                if state is None or state_val != "running":
                    try:
                        self.session.start()
                    except Exception:
                        pass

    def start(self) -> None:
        """Start the underlying session and worker."""
        self.session.start()

    def close(self) -> None:
        """Close the underlying session and worker."""
        self.session.close()

    def inspect_source(self, uri: str) -> SourceInspection:
        """Inspect source shallow metadata without mutating project state."""
        if not uri or not isinstance(uri, str):
            raise ValueError("uri must be a non-empty string")

        try:
            st = os.stat(uri)
        except (FileNotFoundError, NotADirectoryError):
            return SourceInspection(
                exists=False,
                size_bytes=None,
                mtime=None,
                format_guess=None,
                resolved_uri=None,
                device=None,
                inode=None,
                mtime_ns=None,
            )

        mode = st.st_mode
        if stat.S_ISDIR(mode):
            raise IsADirectoryError(f"Source path is a directory: {uri}")
        non_reg = (
            stat.S_ISFIFO(mode)
            or stat.S_ISCHR(mode)
            or stat.S_ISBLK(mode)
            or stat.S_ISSOCK(mode)
        )
        if non_reg or not stat.S_ISREG(mode):
            raise ValueError(f"Source path is not a regular file: {uri}")

        if st.st_size > MAX_SOURCE_BYTES:
            raise OperationError(
                f"Source file size {st.st_size} exceeds limit "
                f"({MAX_SOURCE_BYTES} bytes): {uri}",
                code="source_too_large",
            )

        return (
            self.session.inspect_source_path(uri)
            if hasattr(self.session, "inspect_source_path")
            else inspect_source(uri)
        )

    def load_source(
        self, inspection: SourceInspection
    ) -> tuple[DataSourceRef, DataObjectRef]:
        """Validate and append a csv_enhanced source and execute its read operation."""
        if not inspection.exists or inspection.resolved_uri is None:
            raise OperationError("Source does not exist", code="source_not_found")

        if inspection.format_guess == "hdf5":
            raise OperationError(
                "HDF5 schema guard is unavailable in alpha",
                code="hdf5_schema_guard_unavailable",
            )
        if inspection.format_guess != "csv_enhanced":
            raise OperationError(
                f"Unsupported source format: {inspection.format_guess!r}",
                code="unsupported_source_format",
            )

        uri_path = Path(inspection.resolved_uri)
        if uri_path.suffix.lower() in FORBIDDEN_SCRIPT_SUFFIXES:
            raise OperationError(
                f"Script files cannot be loaded as data sources: "
                f"{inspection.resolved_uri}",
                code="invalid_source_format",
            )

        if (
            inspection.device is None
            or inspection.inode is None
            or inspection.mtime_ns is None
            or inspection.size_bytes is None
        ):
            raise OperationError(
                "Inspection identity metadata unavailable",
                code="inspection_identity_unavailable",
            )

        current = self.inspect_source(inspection.resolved_uri)
        if not current.exists:
            raise OperationError(
                "Source file disappeared before load", code="source_not_found"
            )
        if (
            current.device != inspection.device
            or current.inode != inspection.inode
            or current.mtime_ns != inspection.mtime_ns
            or current.size_bytes != inspection.size_bytes
        ):
            raise OperationError(
                "Source file modified between inspection and load",
                code="source_changed",
            )

        if current.size_bytes is not None and current.size_bytes > MAX_SOURCE_BYTES:
            raise OperationError(
                f"Source file size {current.size_bytes} exceeds limit",
                code="source_too_large",
            )

        source_id = self.project.new_source_id()
        source_ref = DataSourceRef(
            source_id=source_id,
            uri=inspection.resolved_uri,
            format="csv_enhanced",
            size_bytes=inspection.size_bytes,
            mtime=inspection.mtime,
        )
        self.project.sources = (*self.project.sources, source_ref)

        op_id = self.project.new_operation_id()
        out_id = self.project.new_object_id()
        read_op = Operation(
            op_id=op_id,
            operation_id="timeseries.read",
            operation_schema=1,
            inputs={},
            params={"source": {"source_id": source_id}, "format": "csv_enhanced"},
            outputs=(out_id,),
        )
        self.project.graph.add(read_op)

        self._ensure_session_started()
        self.session.replay(targets=(out_id,))

        obj_ref = next((o for o in self.project.objects if o.object_id == out_id), None)
        if obj_ref is None:
            raise OperationError(
                f"Read operation {op_id} did not produce materialized object {out_id}",
                code="materialization_failed",
            )
        return source_ref, obj_ref

    def apply(
        self, op_name: str, input_id: str, params: Mapping[str, Any] | None = None
    ) -> DataObjectRef:
        """Apply a curated TimeSeries operation to an existing object and replay."""
        if op_name not in REGISTRY:
            raise OperationError(
                f"Unknown operation: {op_name!r}", code="invalid_operation"
            )
        input_object = next(
            (obj for obj in self.project.objects if obj.object_id == input_id), None
        )
        if input_object is None:
            raise OperationError(
                f"Input object {input_id!r} not found in project",
                code="object_not_found",
            )

        spec = REGISTRY[op_name]
        if spec.input_roles and input_object.kind != "TimeSeries":
            raise OperationError(
                f"{op_name} requires a TimeSeries; selected object is "
                f"{input_object.kind}",
                code="invalid_input_kind",
            )
        try:
            norm_params = dict(normalize_params(params or {}, spec=spec))
        except ValueError as exc:
            raise OperationError(str(exc), code="invalid_params") from exc

        role = spec.input_roles[0] if spec.input_roles else "self"
        op_id = self.project.new_operation_id()
        out_id = self.project.new_object_id()
        op = Operation(
            op_id=op_id,
            operation_id=op_name,
            operation_schema=1,
            inputs={role: input_id},
            params=norm_params,
            outputs=(out_id,),
        )
        if (
            input_id not in self.project.graph._producer_by_output
            and input_id not in self.project.graph._known_root_object_ids
        ):
            known = {o.object_id for o in self.project.objects} - {
                out for o in self.project.graph.operations for out in o.outputs
            }
            self.project.graph = OperationGraph(
                self.project.graph.operations,
                known_root_object_ids=known,
            )
            self.session.graph = self.project.graph

        self.project.graph.add(op)

        self._ensure_session_started()
        self.session.replay(targets=(out_id,))

        obj_ref = next((o for o in self.project.objects if o.object_id == out_id), None)
        if obj_ref is None:
            raise OperationError(
                f"Operation {op_id} did not materialize object {out_id}",
                code="materialization_failed",
            )
        return obj_ref

    def fetch_preview(
        self, object_id: str, *, preview_stride: int | None = None
    ) -> PreviewData:
        """Fetch array data over shared memory and detach an immutable copy."""
        obj_ref = next(
            (o for o in self.project.objects if o.object_id == object_id), None
        )
        if obj_ref is None:
            raise OperationError(
                f"Object {object_id!r} not found in project",
                code="object_not_found",
            )
        if self.session.client is None:
            raise OperationError(
                "No worker client configured for preview fetch",
                code="worker_unavailable",
            )

        self._ensure_session_started()
        fetched = self.session.client.fetch_array(
            object_id, preview_stride=preview_stride
        )
        copied: Any | None = None
        copy_error: BaseException | None = None
        close_error: BaseException | None = None
        try:
            raw_array = fetched.preview.values
            copied = np.ascontiguousarray(raw_array).copy()
            copied.flags.writeable = False
        except BaseException as exc:
            copy_error = exc

        try:
            fetched.preview.handle.close()
        except BaseException as exc:
            close_error = exc

        release_error: BaseException | None = None
        try:
            self.session.client.release_array(fetched.descriptor.name)
        except BaseException as exc:
            release_error = exc

        shutdown_error: BaseException | None = None
        if release_error is not None:
            copied = None
            try:
                self.session.close()
            except BaseException as exc:
                shutdown_error = exc

        failures = (
            ("Preview copy failed", copy_error),
            ("Preview consumer handle close failed", close_error),
            ("Preview shared-memory release failed", release_error),
            ("Preview release fail-close session shutdown failed", shutdown_error),
        )
        control_flow_error = next(
            (
                error
                for _label, error in failures
                if error is not None and not isinstance(error, Exception)
            ),
            None,
        )
        primary_error: BaseException | None
        if control_flow_error is not None:
            primary_error = control_flow_error
        elif copy_error is not None:
            primary_error = copy_error
        elif close_error is not None:
            primary_error = close_error
        else:
            primary_error = release_error

        if primary_error is not None:
            for label, error in failures:
                if error is not None and error is not primary_error:
                    _add_preview_failure_note(primary_error, label, error)

        if control_flow_error is not None:
            raise control_flow_error

        if release_error is not None:
            if copy_error is not None:
                raise copy_error
            if close_error is not None:
                raise close_error
            raise OperationError(
                "Failed to release preview shared memory",
                code="preview_release_failed",
            ) from release_error

        if copy_error is not None:
            raise copy_error
        if close_error is not None:
            raise close_error

        assert copied is not None
        return PreviewData(ref=obj_ref, values=copied, unit=fetched.unit)

    def fetch_preview_payload(
        self, object_id: str, *, preview_stride: int | None = None
    ) -> PreviewPayload:
        """Fetch one preview and pair it with a complete transient PlotSpec."""
        preview = self.fetch_preview(object_id, preview_stride=preview_stride)
        ref = preview.ref
        display_label = ref.name or ref.object_id
        amplitude_label = f"Amplitude [{preview.unit}]" if preview.unit else "Amplitude"
        plot_id = f"plot-{ref.object_id.removeprefix('obj-')}"

        if ref.kind == "TimeSeries":
            spec = PlotSpec(
                plot_id=plot_id,
                kind="line",
                object_ids=(ref.object_id,),
                xscale="linear",
                yscale="linear",
                xlim=None,
                ylim=None,
                title=display_label,
                xlabel="Time [s]",
                ylabel=amplitude_label,
                legend=True,
                styles={"label": display_label},
            )
        elif ref.kind == "FrequencySeries":
            spec = PlotSpec(
                plot_id=plot_id,
                kind="line",
                object_ids=(ref.object_id,),
                xscale="linear",
                yscale="log",
                xlim=None,
                ylim=None,
                title=display_label,
                xlabel="Frequency [Hz]",
                ylabel=amplitude_label,
                legend=True,
                styles={"label": display_label},
            )
        elif ref.kind == "Spectrogram":
            spec = PlotSpec(
                plot_id=plot_id,
                kind="spectrogram",
                object_ids=(ref.object_id,),
                xscale="linear",
                yscale="linear",
                xlim=None,
                ylim=None,
                title=display_label,
                xlabel="Time [s]",
                ylabel="Frequency [Hz]",
                legend=False,
                styles={"cmap": "viridis"},
            )
        else:
            raise ValueError(f"Unsupported preview kind: {ref.kind!r}")

        return PreviewPayload(preview=preview, spec=spec)

    def export_script(
        self,
        target_path: Path | str,
        *,
        targets: tuple[str, ...] | None = None,
        include_data_writes: bool = False,
    ) -> None:
        """Export the current project operation graph to a Python script."""
        if include_data_writes:
            script_code = export_python(
                self.project, targets=targets, include_data_writes=True
            )
        else:
            script_code = export_python(self.project, targets=targets)
        dest = Path(target_path)
        dest.write_text(script_code, encoding="utf-8")
