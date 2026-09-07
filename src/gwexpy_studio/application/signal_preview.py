"""Native coordinate/complex previews detached from worker-owned buffers."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import replace
from typing import TYPE_CHECKING, Any

import numpy as np

from ..domain.model import PlotSpec
from ..domain.project_v2 import object_from_dict
from ..errors import OperationError
from ..worker.shm import SharedMemoryDescriptor, attach_block
from .preview import PreviewData, PreviewPayload

if TYPE_CHECKING:
    from .signal_host import SignalHost


def default_native_plot(preview: PreviewData) -> PlotSpec:
    """Select Bode for complex spectra and preserve complex components elsewhere."""
    ref = preview.ref
    bode = ref.kind == "FrequencySeries" and np.iscomplexobj(preview.values)
    spectral = ref.kind == "Spectrogram"
    selector = ref.metadata.get("member_selector")
    suffix = (
        ""
        if selector is None
        else "-member-"
        + hashlib.sha256(
            json.dumps(dict(selector), sort_keys=True).encode("utf-8")
        ).hexdigest()[:16]
    )
    label = ref.name or ref.object_id
    unit = preview.unit
    return PlotSpec(
        plot_id=f"plot-{ref.object_id.removeprefix('obj-')}{suffix}",
        kind="bode" if bode else "spectrogram" if spectral else "line",
        object_ids=(ref.object_id,),
        title=label,
        xscale="log" if bode else "linear",
        yscale="linear"
        if bode
        else "log"
        if ref.kind == "FrequencySeries"
        else "linear",
        xlabel="Frequency [Hz]" if ref.kind == "FrequencySeries" else "Time [s]",
        ylabel="Frequency [Hz]"
        if spectral
        else f"Amplitude [{unit}]"
        if unit
        else "Amplitude",
        legend=not spectral,
        styles={"cmap": "viridis"} if spectral else {"label": label},
        selectors={} if selector is None else {ref.object_id: dict(selector)},
    )


class SignalPreviewController:
    """Transport native axes and filter responses without persistent operations."""

    def _detach_bundles(
        self: SignalHost, bundles: list[Mapping[str, Any]]
    ) -> tuple[PreviewData, ...]:
        if self.session.client is None:
            raise OperationError("Worker unavailable", code="worker_unavailable")
        descriptors = []
        try:
            for bundle in bundles:
                for raw in bundle["arrays"].values():
                    if raw is not None:
                        descriptors.append(
                            SharedMemoryDescriptor(
                                **{**raw, "shape": tuple(raw["shape"])}
                            )
                        )
        except Exception:
            # A malformed bundle can hide owned block names. Close the worker so
            # its ownership cleanup handles both known and unparsed descriptors.
            self.session.close()
            raise
        arrays = {}
        primary: BaseException | None = None
        try:
            for descriptor in descriptors:
                attached = attach_block(descriptor)
                try:
                    value = np.ascontiguousarray(attached.values).copy()
                    value.flags.writeable = False
                    arrays[descriptor.name] = value
                finally:
                    attached.handle.close()
        except BaseException as exc:
            primary = exc
        release_error: BaseException | None = None
        for descriptor in descriptors:
            try:
                self.session.client.release_array(descriptor.name)
            except BaseException as exc:
                release_error = exc
        if release_error is not None:
            try:
                self.session.close()
            except BaseException as exc:
                release_error.add_note(f"Worker close failed: {exc}")
            if primary is not None:
                primary.add_note(f"Shared-memory release failed: {release_error}")
            else:
                primary = release_error
        if primary is not None:
            raise primary
        result = []
        for bundle in bundles:
            ref = object_from_dict(bundle["object_ref"])
            named = {
                key: arrays[raw["name"]] if raw is not None else None
                for key, raw in bundle["arrays"].items()
            }
            values = named["values"]
            if values is None:
                raise ValueError("Preview values descriptor is required")
            result.append(
                PreviewData(
                    ref=ref,
                    unit=bundle["arrays"]["values"]["unit"],
                    values=values,
                    x_coordinates=named.get("x_coordinates"),
                    y_coordinates=named.get("y_coordinates"),
                )
            )
        return tuple(result)

    def fetch_member_preview_payload(
        self: SignalHost, object_id: str, selector: Mapping[str, Any] | None = None
    ) -> PreviewPayload | None:
        """Preview a leaf with original coordinate arrays; browsing adds no DAG node."""
        ref = self._object(object_id)
        if selector is None and ref.kind not in {
            "TimeSeries",
            "FrequencySeries",
            "Spectrogram",
        }:
            return None
        bundle = self._signal_request(
            "preview_data", {"object_id": object_id, "selector": selector}
        )
        preview = self._detach_bundles([bundle])[0]
        spec = default_native_plot(preview)
        spec = next(
            (saved for saved in self.project.plots if saved.plot_id == spec.plot_id),
            spec,
        )
        self.set_plot_spec(spec)
        return PreviewPayload(preview=preview, spec=spec)

    def filter_preview(
        self: SignalHost,
        op_name: str,
        inputs: Mapping[str, Mapping[str, Any]],
        params: Mapping[str, Any],
        generation: int = 0,
        recorded_object_id: str | None = None,
        recorded_selector: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Preview a proposed or recorded filter without materializing output data."""
        from ..ops.registry import REGISTRY
        from ..ops.spec import normalize_params

        if op_name not in {
            f"timeseries.{name}"
            for name in ("highpass", "lowpass", "bandpass", "notch", "zpk")
        }:
            raise OperationError("Select a supported filter", code="invalid_operation")
        for handle in inputs.values():
            if not self._object(str(handle["object_id"])).kind.startswith("TimeSeries"):
                raise OperationError(
                    "Filters require time series", code="invalid_input_kind"
                )
        payload: dict[str, Any] = {
            "op_name": op_name,
            "inputs": dict(inputs),
            "params": dict(normalize_params(params, spec=REGISTRY[op_name])),
            "generation": generation,
        }
        if recorded_object_id is not None:
            ref = self._object(recorded_object_id)
            execution = next(
                (
                    record
                    for record in reversed(self.project.executions)
                    if record.op_id == ref.produced_by and record.status == "succeeded"
                ),
                None,
            )
            if execution is None or not execution.details.get("filter_recipes"):
                raise OperationError(
                    "No recorded filter coefficients available", code="invalid_params"
                )
            details = dict(execution.details)
            if recorded_selector is not None:
                count = int(ref.metadata.get("member_count", 0))
                index = None
                for offset in range(0, count, 100):
                    page = self.list_members(recorded_object_id, offset, 100)
                    index = next(
                        (
                            offset + i
                            for i, member in enumerate(page)
                            if dict(member.selector) == dict(recorded_selector)
                        ),
                        None,
                    )
                    if index is not None:
                        break
                if index is None:
                    raise OperationError(
                        "Recorded member not found", code="object_not_found"
                    )
                details["filter_recipes"] = [details["filter_recipes"][index]]
            payload["recorded_details"] = details
        result = self._signal_request("filter_preview", payload)
        previews = self._detach_bundles(result["members"])
        description = result.get("description", "Filter response")
        pairs = tuple(
            PreviewPayload(
                preview=preview,
                spec=replace(
                    default_native_plot(preview),
                    title=f"{member['label']} — {description}",
                ),
            )
            for preview, member in zip(previews, result["members"], strict=True)
        )
        if not pairs:
            raise OperationError("No filter response available", code="invalid_params")
        return {
            "filter_preview": pairs[0],
            "filter_previews": pairs,
            "generation": result.get("generation", generation),
            "members": tuple(member["label"] for member in result["members"]),
        }
