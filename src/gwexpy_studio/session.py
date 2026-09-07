"""Headless Studio session façade implementation."""

from __future__ import annotations

import gc
import time
import uuid
import warnings
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from .domain.graph import OperationGraph
from .domain.project import ExecutionRecord, Project
from .domain.project_v2 import object_from_dict
from .errors import OperationError
from .ops.source import SourceInspection, inspect_source
from .worker.client import WorkerClient, WorkerLifecycle
from .worker.protocol import UUID4, RequestEnvelope


class StudioSession:
    """Coordinate a project graph and injected worker client."""

    def __init__(
        self,
        *,
        project: Project | None = None,
        graph: OperationGraph | None = None,
        client: WorkerClient | None = None,
        client_factory: Callable[..., WorkerClient] | None = None,
        source_inspector: Callable[[str], SourceInspection] | None = None,
    ) -> None:
        """Store session dependencies without importing gwexpy."""
        self.project = project
        self.graph = (
            graph
            if graph is not None
            else (project.graph if project is not None else None)
        )
        self.client = client
        self.client_factory = client_factory
        self.source_inspector = source_inspector
        self._successful_ops: set[str] = set()

    def inspect_source(self, source_id: str) -> SourceInspection:
        """Inspect the project source identified by source_id.

        Compares the freshly probed file mtime against the mtime stored on
        the project record at load time; if the file was reloaded or
        changed since the session was started, a UserWarning is issued.
        """
        if self.project is None:
            raise KeyError("No project associated with this session")

        source = next(
            (s for s in self.project.sources if s.source_id == source_id),
            None,
        )
        if source is None:
            raise KeyError(f"Source with id {source_id!r} not found in project")

        if self.source_inspector is not None:
            inspection = self.source_inspector(source.uri)
        elif self.client is not None and self.client.state == WorkerLifecycle.RUNNING:
            req: RequestEnvelope = {
                "protocol": 2,
                "request_id": UUID4(str(uuid.uuid4())),
                "type": "inspect_source",
                "payload": {"uri": source.uri},
            }
            resp = self.client.request(req)
            raw_payload = resp.get("payload", {})
            payload = raw_payload if isinstance(raw_payload, dict) else {}
            inspection = SourceInspection(
                exists=bool(payload.get("exists", False)),
                size_bytes=(
                    int(payload["size_bytes"])
                    if payload.get("size_bytes") is not None
                    else None
                ),
                mtime=(
                    float(payload["mtime"])
                    if payload.get("mtime") is not None
                    else None
                ),
                format_guess=payload.get("format_guess"),  # type: ignore[arg-type]
                resolved_uri=payload.get("resolved_uri"),
                device=(
                    int(payload["device"])
                    if payload.get("device") is not None
                    else None
                ),
                inode=(
                    int(payload["inode"]) if payload.get("inode") is not None else None
                ),
                mtime_ns=(
                    int(payload["mtime_ns"])
                    if payload.get("mtime_ns") is not None
                    else None
                ),
            )
        else:
            inspection = inspect_source(source.uri)

        if (
            source.mtime is not None
            and inspection.mtime is not None
            and inspection.mtime != source.mtime
        ):
            warnings.warn(
                f"source mtime changed for {source_id}: "
                f"project was {source.mtime}, file is {inspection.mtime}",
                UserWarning,
                stacklevel=2,
            )

        return inspection

    def _last_execution_status(self, project: Project) -> dict[str, str]:
        """Map each operation to the status of its most recent execution."""
        last_exec_status: dict[str, str] = {}
        for rec in project.executions:
            last_exec_status[rec.op_id] = rec.status
        return last_exec_status

    def _default_targets(
        self, graph: OperationGraph, project: Project
    ) -> tuple[str, ...]:
        """Derive replay targets when the caller named none.

        While the project holds materialized objects the targets are its
        terminal ones -- the objects no operation consumes.  Contracts
        I-S4-001/002 and I-E2E-001/002 pin that rule: a declared but
        unmaterialized leaf (the output of an operation that failed, or
        that has never run) must stay out of the default closure.

        When nothing is materialized at all that rule has no input, and
        deriving an empty target set from it turned a whole-graph replay
        into a silent success.  ADR-0008 makes the OperationGraph the
        source of truth, so a never-executed or freshly loaded project
        falls back to the graph's own terminal outputs and replays every
        declared operation.  Terminal objects whose ancestor closure
        contains an operation whose latest execution failed are skipped
        even then: ADR-0006 keeps a failed operation in the provenance
        record instead of retrying it implicitly, and an explicit
        ``targets`` argument remains the way to retry one.
        """
        if project.history.initialized:
            from .domain.history import active_targets

            return active_targets(project)
        operations = graph.operations
        consumed = {in_id for op in operations for in_id in op.inputs.values()}

        if project.objects:
            materialized_targets = tuple(
                obj.object_id
                for obj in project.objects
                if obj.object_id not in consumed
            )
            return materialized_targets or (project.objects[-1].object_id,)

        failed_op_ids = {
            op_id
            for op_id, status in self._last_execution_status(project).items()
            if status == "failed"
        }
        target_ids: list[str] = []
        for op in operations:
            for object_id in op.outputs:
                if object_id in consumed:
                    continue
                if failed_op_ids and any(
                    ancestor.op_id in failed_op_ids
                    for ancestor in graph.ancestors((object_id,))
                ):
                    continue
                target_ids.append(object_id)
        return tuple(target_ids)

    def replay(
        self,
        *,
        targets: tuple[str, ...] | None = None,
        clock: Callable[[], str] | None = None,
        refresh_metadata: bool = False,
        progress: Callable[[dict[str, Any]], None] | None = None,
        check_cancel: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        """Replay graph operations and update project execution records.

        ``targets`` names the object IDs to materialize; their ancestor
        closure is executed in graph order.  When it is omitted the
        targets are derived from the graph (see ``_default_targets``).
        ``clock`` injects the ``ExecutionRecord.started_at`` source and
        defaults to the real UTC wall clock.

        Both a default (whole-graph) replay and an explicit ``targets``
        replay only ever *append* new ``DataObjectRef`` entries to
        ``Project.objects`` for the executed closure's outputs -- neither
        ever removes or mutates a ``DataObjectRef`` belonging to a branch
        that this replay did not execute (ADR-0006: operations are
        non-destructive, and ``Project.objects`` is the persisted
        scientific record, not a mirror of the worker's current resident
        object set).
        """
        if self.project is None or self.graph is None:
            raise OperationError(
                "Replay requires both a project and an operation graph",
                code="no_replay_target",
            )

        is_default = targets is None
        if targets is None:
            target_ids = self._default_targets(self.graph, self.project)
            if (
                not target_ids
                and self.graph.operations
                and not self.project.history.initialized
            ):
                raise OperationError(
                    "No replayable target: the project has materialized no "
                    "object and every declared terminal object depends on an "
                    "operation whose last execution failed",
                    code="no_replay_target",
                )
        else:
            target_ids = tuple(targets)

        ancestors = self.graph.ancestors(target_ids)
        ancestor_ids = {op.op_id for op in ancestors}
        ordered_ops = [op for op in self.graph.operations if op.op_id in ancestor_ids]

        # Check if any ancestor operation has previously failed
        # and its output is missing
        existing_obj_ids = {o.object_id for o in self.project.objects}
        last_exec_status = self._last_execution_status(self.project)

        for op in ordered_ops:
            for in_id in op.inputs.values():
                if in_id not in existing_obj_ids:
                    producing_op = next(
                        (o for o in self.graph.operations if in_id in o.outputs),
                        None,
                    )
                    if (
                        producing_op is not None
                        and last_exec_status.get(producing_op.op_id) == "failed"
                    ):
                        raise OperationError(
                            f"Input object {in_id!r} not available because "
                            f"operation {producing_op.op_id} failed",
                            code="object_not_found",
                        )

        current_process = getattr(self.client, "_process", None)
        if current_process is not getattr(self, "_last_worker_process", None):
            self._successful_ops.clear()
            self._last_worker_process = current_process

        if is_default:
            ops_to_run = ordered_ops
        else:
            ops_to_run = [
                op for op in ordered_ops if op.op_id not in self._successful_ops
            ]

        sources_map = {s.source_id: s.uri for s in self.project.sources}

        for op_index, op in enumerate(ops_to_run):
            if check_cancel is not None:
                check_cancel()
            if progress is not None:
                progress(
                    {
                        "completed": op_index,
                        "total": len(ops_to_run),
                        "label": op.operation_id,
                    }
                )
            op_params = dict(op.params)
            if "source" in op_params and isinstance(op_params["source"], dict):
                src_id = op_params["source"].get("source_id", "")
                op_params["source"] = sources_map.get(src_id, src_id)

            op_payload: dict[str, Any] = {
                "op_id": op.op_id,
                "operation_id": op.operation_id,
                "operation_schema": op.operation_schema,
                "inputs": dict(op.inputs),
                "params": op_params,
                "outputs": list(op.outputs),
            }

            req: RequestEnvelope = {
                "protocol": 2,
                "request_id": UUID4(str(uuid.uuid4())),
                "type": "execute",
                "payload": {
                    "operation": op_payload,
                    "graph_id": self.project.project_id,
                    "operation_id": op.op_id,
                    "input_object_ids": list(op.inputs.values()),
                    "output_object_id": op.outputs[0] if op.outputs else "",
                },
            }

            recorded = next(
                (
                    record.details
                    for record in reversed(self.project.executions)
                    if record.op_id == op.op_id
                    and record.status == "succeeded"
                    and record.details.get("filter_recipes")
                ),
                None,
            )
            if recorded is not None:
                req["payload"]["recorded_details"] = dict(recorded)

            if self.client is None:
                # No worker is attached, so the operation was never sent and
                # never executed. Previously this branch fabricated a
                # succeeded ExecutionRecord and added op.op_id to
                # _successful_ops, which permanently hid the fact that the
                # operation was skipped (a later targeted replay would treat
                # it as already-done) while never materializing the output
                # DataObjectRef. Raise before any ExecutionRecord is
                # appended so no record of an un-executed operation is ever
                # persisted. code="worker_unavailable" is new: existing
                # codes (no_replay_target, object_not_found,
                # operation_failed, worker_crashed) all describe a request
                # that reached a worker and failed there; this is the
                # distinct case where no request was ever sent.
                raise OperationError(
                    f"Cannot execute operation {op.op_id!r}: no worker "
                    "client is configured for this session",
                    code="worker_unavailable",
                )

            started_at = clock() if clock is not None else datetime.now(UTC).isoformat()
            t0 = time.monotonic()
            resp: dict[str, Any] = dict(self.client.request(req))  # type: ignore[arg-type]
            is_success = resp.get("type") == "result"
            err_val: dict[str, Any] | None = (
                None
                if is_success
                else resp.get(
                    "error",
                    {
                        "code": resp.get("code", "operation_failed"),
                        "message": resp.get("message", ""),
                    },
                )
            )
            duration = max(time.monotonic() - t0, 0.001)

            response_payload = resp.get("payload", {})
            if not isinstance(response_payload, dict):
                raise OperationError(
                    "Worker execution response payload must be an object"
                )
            details = dict(response_payload.get("details", {}))
            previous = next(
                (o for o in self.project.objects if o.object_id in op.outputs), None
            )
            if refresh_metadata and previous is not None:
                from .domain.project_v2 import record_dict

                details["previous_object_ref"] = record_dict(previous)
            exec_record = ExecutionRecord(
                execution_id=str(uuid.uuid4()),
                op_id=op.op_id,
                started_at=started_at,
                duration_s=duration,
                status="succeeded" if is_success else "failed",
                error=err_val,
                warnings=tuple(response_payload.get("warnings", ())),
                details=details,
                environment=dict(response_payload.get("environment", {})),
            )
            self.project.executions = (*self.project.executions, exec_record)

            if is_success:
                self._successful_ops.add(op.op_id)
                if self.project is not None:
                    obj_data = resp.get("payload", {}).get("object_ref")  # type: ignore[union-attr]
                    if obj_data and (
                        refresh_metadata
                        or not any(
                            o.object_id == obj_data["object_id"]
                            for o in self.project.objects
                        )
                    ):
                        new_ref = object_from_dict(
                            {
                                "name": None,
                                "channel": None,
                                "axes": {},
                                **obj_data,
                                "produced_by": op.op_id,
                            }
                        )
                        if refresh_metadata and previous is not None:
                            self.project.objects = tuple(
                                new_ref if ref.object_id == new_ref.object_id else ref
                                for ref in self.project.objects
                            )
                        else:
                            self.project.objects = (*self.project.objects, new_ref)

            if not is_success:
                code = str(resp.get("code", "operation_failed"))
                msg = str(resp.get("message", f"Operation {op.op_id} failed"))
                raise OperationError(msg, code=code)

        if progress is not None:
            progress(
                {
                    "completed": len(ops_to_run),
                    "total": len(ops_to_run),
                    "label": "Completed",
                }
            )
        return {"object_ids": target_ids}

    def start(self) -> None:
        """Start the session and underlying worker client."""
        # Left as a silent no-op when neither client nor client_factory is
        # configured. This is deliberate, not an oversight: unlike the
        # replay() worker_unavailable fix above, start() does not fabricate
        # any false success record -- it simply has nothing to start. A
        # session with no client can still be legitimately constructed and
        # inspected (e.g. graph/domain-only usage that never calls
        # replay()), so failing eagerly here would reject sessions that
        # never actually needed a worker. The now-explicit
        # OperationError(code="worker_unavailable") raised by replay()
        # already surfaces the missing-worker condition at the point where
        # it actually matters (an attempt to execute an operation), so
        # start() is left alone rather than widening this fix's scope.
        if self.client is None and self.client_factory is not None:
            self.client = self.client_factory()

        if self.client is not None and self.client.state != WorkerLifecycle.RUNNING:
            self.client.start()

    def close(self) -> None:
        """Close the session and shut down the underlying worker client."""
        if self.client is not None and self.client.state != WorkerLifecycle.CLOSED:
            self.client.shutdown()
        gc.collect()
