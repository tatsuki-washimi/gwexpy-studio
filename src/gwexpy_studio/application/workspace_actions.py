"""Atomic user gestures and availability guards over append-only provenance."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any, TypeVar

from ..domain.history import (
    active_object_ids,
    begin_history,
    can_redo,
    can_undo,
    commit_history,
    redo_history,
    selected_handles,
    undo_history,
)
from ..domain.model import ActivityRecord, DataObjectRef, DataSourceRef, PlotSpec
from ..errors import OperationError
from ..ops.source import SourceInspection
from .alpha import AlphaController
from .workspace_document import WorkspaceDocument, detached

T = TypeVar("T")


class WorkspaceActions(WorkspaceDocument, AlphaController):
    """Add user-action boundaries without changing the native scientific APIs."""

    def _require_available(self, object_id: str) -> None:
        if object_id not in active_object_ids(self.project):
            raise OperationError("Selected data is not active", code="object_not_found")
        if object_id not in self._resident:
            raise OperationError(
                "Restore data before using this object", code="restore_required"
            )

    def _object(self, object_id: str) -> DataObjectRef:
        if self._group_depth == 0:
            self._require_available(object_id)
        return super()._object(object_id)

    def fetch_preview(self, object_id: str, *, preview_stride: int | None = None):
        """Only preview active data resident in the current worker generation."""
        self._require_available(object_id)
        return super().fetch_preview(object_id, preview_stride=preview_stride)

    def _gesture(
        self,
        label: str,
        execute: Callable[[], T],
        outputs: Callable[[T], tuple[str, ...]],
        *,
        selection: tuple[Mapping[str, Any], ...] | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> T:
        saved_selection = self.project.ui_state.get("selection")
        if isinstance(saved_selection, Mapping):
            selected_id = saved_selection.get("object_id")
            selection = (
                (saved_selection,)
                if selected_id in active_object_ids(self.project)
                else ()
            )
        if selection is None:
            selection = selected_handles(self.project)
        pending = begin_history(self.project, label, selected_handles=selection)
        previous_ids = {obj.object_id for obj in self.project.objects}
        self._intent("analysis", {"label": label, **dict(details or {})})
        self._group_depth += 1
        self._cancel_client = self.session.client
        self._cancel_event.clear()
        try:
            result = execute()
            wanted = outputs(result)
            commit_history(
                self.project,
                pending,
                output_ids=wanted,
                selected_handles=({"object_id": wanted[-1]},) if wanted else (),
            )
            self._resident.update(
                obj.object_id
                for obj in self.project.objects
                if obj.object_id not in previous_ids
            )
            self._needs_restore = not active_object_ids(self.project) <= self._resident
            return result
        except BaseException as exc:
            new_ids = {obj.object_id for obj in self.project.objects} - previous_ids
            for object_id in new_ids:
                try:
                    self._signal_request("delete_object", {"object_id": object_id})
                except Exception as cleanup:
                    exc.add_note(f"Staged data cleanup failed: {cleanup}")
                    self.session.close()
                    self._resident.clear()
                    break
            self._resident.difference_update(new_ids)
            self.session._successful_ops.difference_update(
                op.op_id
                for op in self.project.graph.operations
                if op.op_id not in pending.existing_operation_ids
            )
            if getattr(exc, "code", None) in {
                "worker_crashed",
                "worker_timeout",
                "timeout",
            }:
                self._append_interruption(self._pending_intent or {})
                self._resident.clear()
            self._needs_restore = not active_object_ids(self.project) <= self._resident
            raise
        finally:
            self._group_depth -= 1
            self._cancel_client = None
            self._pending_intent = None
            self._changed()

    def _fingerprint(
        self,
        paths: list[str],
        *,
        max_bytes: int = 536870912,
        max_entries: int = 10000,
        expected: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "mode": "fingerprint",
            "request": {
                "paths": paths,
                "max_bytes": max_bytes,
                "max_entries": max_entries,
            },
        }
        if expected is not None:
            payload["expected_manifest"] = dict(expected)
        return dict(self._signal_request("inspect_io", payload)["source_manifest"])

    def read_io(self, inspection: Mapping[str, Any]) -> tuple[DataObjectRef, ...]:
        """Read the entire reviewed input group before publishing any output."""
        request = inspection["request"]
        paths = list(request["paths"])

        def execute() -> tuple[DataObjectRef, ...]:
            evidence = self._fingerprint(
                paths,
                max_bytes=request.get("max_bytes", 536870912),
                max_entries=request.get("max_entries", 10000),
            )
            anchor = self.project.new_operation_id()
            result = AlphaController.read_io(self, inspection)
            self._fingerprint(
                paths,
                expected=evidence,
                max_bytes=evidence["max_bytes"],
                max_entries=evidence["max_entries"],
            )
            self.project.source_manifests = {
                **self.project.source_manifests,
                anchor: evidence,
            }
            return result

        return self._gesture(
            "Read Data",
            execute,
            lambda result: tuple(obj.object_id for obj in result),
            details={"paths": paths},
        )

    def load_source(
        self, inspection: SourceInspection
    ) -> tuple[DataSourceRef, DataObjectRef]:
        """Read a legacy CSV source with the same persistent action boundary."""

        def execute() -> tuple[DataSourceRef, DataObjectRef]:
            paths = [str(inspection.resolved_uri)]
            evidence = self._fingerprint(paths)
            anchor = self.project.new_operation_id()
            result = AlphaController.load_source(self, inspection)
            self._fingerprint(paths, expected=evidence)
            self.project.source_manifests = {
                **self.project.source_manifests,
                anchor: evidence,
            }
            return result

        return self._gesture(
            "Read Data",
            execute,
            lambda result: (result[1].object_id,),
            details={"inspection": asdict(inspection)},
        )

    def apply(
        self, op_name: str, input_id: str, params: Mapping[str, Any] | None = None
    ) -> DataObjectRef:
        """Apply one curated operation to an available active input."""
        self._require_available(input_id)
        return self._gesture(
            op_name,
            lambda: AlphaController.apply(self, op_name, input_id, params),
            lambda obj: (obj.object_id,),
            selection=({"object_id": input_id},),
            details={"params": dict(params or {}), "input_id": input_id},
        )

    def apply_multi(
        self,
        op_name: str,
        inputs: Mapping[str, Mapping[str, Any]],
        params: Mapping[str, Any] | None = None,
    ) -> DataObjectRef:
        """Group member extraction and the native calculation as one action."""
        for handle in inputs.values():
            self._require_available(str(handle["object_id"]))
        return self._gesture(
            op_name,
            lambda: AlphaController.apply_multi(self, op_name, inputs, params),
            lambda obj: (obj.object_id,),
            selection=tuple(inputs.values()),
            details={"params": dict(params or {}), "inputs": dict(inputs)},
        )

    def _execute_signal(
        self, name: str, inputs: Mapping[str, str], params: Mapping[str, Any]
    ) -> DataObjectRef:
        self._group_depth += 1
        try:
            result = super()._execute_signal(name, inputs, params)
        finally:
            self._group_depth -= 1
        self._resident.add(result.object_id)
        return result

    def _materialize_handle(self, handle: Mapping[str, Any]) -> str:
        parent = str(handle["object_id"])
        selector = handle.get("selector")
        if selector is None:
            return parent
        active = active_object_ids(self.project)
        for op in self.project.graph.operations:
            if (
                op.operation_id == "data.extract"
                and op.inputs.get("self") == parent
                and op.params.get("selector") == selector
                and op.outputs[0] in self._resident
                and op.outputs[0] in active
            ):
                return op.outputs[0]
        return self._execute_signal(
            "data.extract", {"self": parent}, {"selector": selector}
        ).object_id

    def undo_analysis(self) -> None:
        """Return to the prior active heads without removing provenance."""
        if not can_undo(self.project):
            raise OperationError("No analysis to undo", code="undo_unavailable")
        self._intent("undo", {})
        undo_history(self.project)
        self._pending_intent = None
        self._needs_restore = not active_object_ids(self.project) <= self._resident
        self._changed()

    def redo_analysis(self) -> None:
        """Reactivate one action only when all required values are resident."""
        if not can_redo(self.project):
            raise OperationError("No analysis to redo", code="redo_unavailable")
        candidate = detached(self.project)
        redo_history(candidate)
        if not active_object_ids(candidate) <= self._resident:
            raise OperationError(
                "Restore redo data before continuing", code="restore_required"
            )
        self._intent("redo", {})
        redo_history(self.project)
        self._pending_intent = None
        self._changed()

    def set_plot_spec(self, spec: PlotSpec) -> None:
        """Persist the current figure settings independently of analysis undo."""
        before = self.project.plots
        # View declarations need valid object metadata, without resident arrays.
        self._group_depth += 1
        try:
            super().set_plot_spec(spec)
        finally:
            self._group_depth -= 1
        if self.project.plots != before:
            self._changed()

    def write_data(
        self,
        object_id: str,
        request: Mapping[str, Any],
        selector: Mapping[str, Any] | None = None,
    ) -> ActivityRecord:
        """Record the external write intent without adding scientific undo."""
        self._require_available(object_id)
        self._intent(
            "write_data",
            {
                "target": request.get("paths", [None])[0],
                "object_id": object_id,
                "selector": selector,
                "request": dict(request),
            },
        )
        before = len(self.project.activities)
        self._cancel_event.clear()
        self._cancel_client = self.session.client
        self._group_depth += 1
        try:
            return super().write_data(object_id, request, selector)
        except Exception as exc:
            cause = exc.__cause__ or exc
            if getattr(cause, "code", None) in {
                "worker_crashed",
                "worker_timeout",
                "timeout",
                "operation_cancelled",
            }:
                self._resident.clear()
                self._needs_restore = bool(active_object_ids(self.project))
                self.project.activities = tuple(
                    replace(
                        record,
                        status="unknown",
                        error={"code": "output_unconfirmed", "message": str(cause)},
                    )
                    if index >= before
                    else record
                    for index, record in enumerate(self.project.activities)
                )
                if len(self.project.activities) == before:
                    self._append_interruption(self._pending_intent or {})
                raise cause from None
            raise
        finally:
            self._group_depth -= 1
            self._cancel_client = None
            activities = list(self.project.activities)
            for index in range(before, len(activities)):
                record = activities[index]
                activities[index] = replace(
                    record,
                    details={
                        **record.details,
                        "logical_handle": {
                            "object_id": object_id,
                            "selector": selector,
                        },
                    },
                )
            self.project.activities = tuple(activities)
            self._pending_intent = None
            self._changed()

    def export_script(
        self,
        target_path: Path | str,
        *,
        targets: tuple[str, ...] | None = None,
        include_data_writes: bool = False,
    ) -> None:
        """Export active analysis while retaining an interrupted-write marker."""
        self._intent("export", {"target": str(target_path)})
        try:
            super().export_script(
                target_path, targets=targets, include_data_writes=include_data_writes
            )
        finally:
            self._pending_intent = None
            self._checkpoint()
