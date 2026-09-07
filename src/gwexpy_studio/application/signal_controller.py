"""Qt-free orchestration for native operations and lazy container members."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any, cast

from ..domain.graph import OperationGraph
from ..domain.model import DataObjectRef, MemberRef, Operation, PlotSpec
from ..domain.project import Project
from ..domain.project_v2 import record_dict
from ..errors import OperationError
from ..ops.spec import normalize_params
from ..session import StudioSession
from ..worker.protocol import PROTOCOL_VERSION, RequestEnvelope
from .signal_io_controller import SignalIOController
from .signal_preview import SignalPreviewController


class SignalController(SignalIOController, SignalPreviewController):
    """Append semantic operations only after validating selected input kinds."""

    project: Project
    session: StudioSession

    def _signal_request(self, kind: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        self.session.start()
        if self.session.client is None:
            raise OperationError("Worker unavailable", code="worker_unavailable")
        request = cast(
            RequestEnvelope,
            {
                "protocol": PROTOCOL_VERSION,
                "request_id": str(uuid.uuid4()),
                "type": kind,
                "payload": dict(payload),
            },
        )
        response = self.session.client.request(request)
        if response["type"] == "error":
            raise OperationError(str(response["message"]), code=str(response["code"]))
        return dict(response["payload"])

    def _object(self, object_id: str) -> DataObjectRef:
        for ref in self.project.objects:
            if ref.object_id == object_id:
                return ref
        raise OperationError(f"Object {object_id!r} not found", code="object_not_found")

    def _execute_signal(
        self, name: str, inputs: Mapping[str, str], params: Mapping[str, Any]
    ) -> DataObjectRef:
        operation = Operation(
            op_id=self.project.new_operation_id(),
            operation_id=name,
            operation_schema=1,
            inputs=dict(inputs),
            params=dict(params),
            outputs=(self.project.new_object_id(),),
        )
        produced = {out for op in self.project.graph.operations for out in op.outputs}
        roots = {obj.object_id for obj in self.project.objects} - produced
        self.project.graph = OperationGraph(
            self.project.graph.operations, known_root_object_ids=roots
        )
        self.session.graph = self.project.graph
        self.project.graph.add(operation)
        self.session.replay(targets=operation.outputs)
        return self._object(operation.outputs[0])

    def _materialize_handle(self, handle: Mapping[str, Any]) -> str:
        parent_id = str(handle["object_id"])
        self._object(parent_id)
        selector = handle.get("selector")
        if selector is None:
            return parent_id
        # Reuse a successful extraction with the same immutable parent/selector.
        for op in self.project.graph.operations:
            if (
                op.operation_id == "data.extract"
                and op.inputs.get("self") == parent_id
                and op.params.get("selector") == selector
                and any(ref.object_id in op.outputs for ref in self.project.objects)
            ):
                return op.outputs[0]
        return self._execute_signal(
            "data.extract", {"self": parent_id}, {"selector": selector}
        ).object_id

    def apply_multi(
        self,
        op_name: str,
        inputs: Mapping[str, Mapping[str, Any]],
        params: Mapping[str, Any] | None = None,
    ) -> DataObjectRef:
        """Apply a native operation to explicitly selected parents or members."""
        from ..ops.registry import REGISTRY

        if op_name not in REGISTRY or op_name in {"data.read", "timeseries.read"}:
            raise OperationError(
                f"Unknown operation {op_name!r}", code="invalid_operation"
            )
        spec = REGISTRY[op_name]
        roles = set(spec.input_roles) | set(spec.optional_input_roles)
        if not set(spec.input_roles) <= set(inputs) or set(inputs) - roles:
            raise OperationError(
                "Missing or unexpected input roles", code="invalid_params"
            )
        for role, handle in inputs.items():
            if not isinstance(handle, Mapping) or not isinstance(
                handle.get("object_id"), str
            ):
                raise OperationError("Select an input object", code="invalid_params")
            kind = self._object(handle["object_id"]).kind
            if handle.get("selector") is not None:
                if not isinstance(handle["selector"], Mapping):
                    raise OperationError(
                        "Invalid member selector", code="invalid_params"
                    )
                for suffix in ("Dict", "List", "Matrix"):
                    kind = kind.removesuffix(suffix)  # type: ignore[assignment]
            accepted = spec.accepted_input_kinds
            if accepted and kind not in accepted.get(role, ()):
                raise OperationError(
                    f"{op_name} cannot use {kind} as {role}", code="invalid_input_kind"
                )
            if (
                accepted is None
                and op_name.startswith("timeseries.")
                and kind != "TimeSeries"
            ):
                raise OperationError(
                    f"{op_name} requires TimeSeries", code="invalid_input_kind"
                )
        try:
            normalized = normalize_params(params or {}, spec=spec)
            if op_name.startswith("data.") and op_name != "data.extract":
                scalar = normalized.get("operand_mode") == "scalar"
                if scalar and ("other" in inputs or normalized.get("scalar") is None):
                    raise ValueError("Choose one scalar operand")
                if not scalar and "other" not in inputs:
                    raise ValueError("Select the second data operand")
        except (ValueError, TypeError) as exc:
            raise OperationError(str(exc), code="invalid_params") from exc
        resolved = {
            role: self._materialize_handle(handle) for role, handle in inputs.items()
        }
        return self._execute_signal(op_name, resolved, normalized)

    def list_members(
        self, object_id: str, offset: int = 0, limit: int = 100
    ) -> tuple[MemberRef, ...]:
        """Describe a page of children without adding objects or operations."""
        self._object(object_id)
        result = self._signal_request(
            "list_members", {"object_id": object_id, "offset": offset, "limit": limit}
        )
        return tuple(
            MemberRef(**{**item, "shape": tuple(item["shape"])})
            for item in result["members"]
        )

    def set_plot_spec(self, spec: PlotSpec) -> None:
        """Keep selected display settings independently of scientific history."""
        if not isinstance(spec, PlotSpec):
            raise TypeError("Plot settings require a PlotSpec")
        for object_id in spec.object_ids:
            self._object(object_id)
        # Apply the same schema validation used when saving/loading a project.
        from ..domain.project_v2 import plot_options_from_dict

        plot_options_from_dict(record_dict(spec), "plot")
        self.project.plots = tuple(
            p for p in self.project.plots if p.plot_id != spec.plot_id
        ) + (spec,)
