"""Contract coverage for complete alpha preview payload plot specifications."""

from __future__ import annotations

from dataclasses import FrozenInstanceError, fields
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, call

import numpy as np
import pytest

import gwexpy_studio.application as application
from gwexpy_studio.application import AlphaController, PreviewData
from gwexpy_studio.domain.model import DataObjectRef, ObjectKind, PlotSpec
from gwexpy_studio.domain.project import Project
from gwexpy_studio.plotting.preview_renderer import render_preview
from gwexpy_studio.session import StudioSession
from gwexpy_studio.worker.shm import (
    FetchedArray,
    SharedMemoryDescriptor,
    SharedMemoryPreview,
)

if TYPE_CHECKING:
    from gwexpy_studio.application import PreviewPayload


def _ref(
    kind: ObjectKind,
    number: int,
    *,
    name: str | None,
    unit: str = "m",
) -> DataObjectRef:
    shape: tuple[int, ...]
    if kind == "FrequencySeries":
        shape = (4,)
        axes = {
            "f0": {"value": 1.0, "unit": "Hz"},
            "df": {"value": 0.5, "unit": "Hz"},
        }
    elif kind == "Spectrogram":
        shape = (3, 4)
        axes = {
            "t0": {"value": 1.0, "unit": "s"},
            "dt": {"value": 1.0, "unit": "s"},
            "f0": {"value": 10.0, "unit": "Hz"},
            "df": {"value": 2.0, "unit": "Hz"},
        }
    else:
        shape = (4,)
        axes = {
            "t0": {"value": 1.0, "unit": "s"},
            "dt": {"value": 1.0, "unit": "s"},
        }
    return DataObjectRef(
        object_id=f"obj-{number}",
        kind=kind,
        shape=shape,
        dtype="float64",
        unit=unit,
        name=name,
        axes=axes,
    )


def _values(ref: DataObjectRef) -> np.ndarray:
    return np.arange(1, int(np.prod(ref.shape)) + 1, dtype=np.float64).reshape(
        ref.shape
    )


def _fetched(
    ref: DataObjectRef,
    handle: Any,
    *,
    shm_name: str,
) -> FetchedArray:
    values = _values(ref)
    descriptor = SharedMemoryDescriptor(
        name=shm_name,
        dtype=values.dtype.name,
        shape=values.shape,
        nbytes=values.nbytes,
        unit=ref.unit,
    )
    return FetchedArray(
        descriptor=descriptor,
        preview=SharedMemoryPreview(
            values=values,
            coordinates=np.arange(values.size, dtype=np.float64),
            handle=handle,
        ),
        unit=ref.unit,
    )


def _controller_for(
    refs: tuple[DataObjectRef, ...],
) -> tuple[AlphaController, Project, MagicMock, dict[str, MagicMock]]:
    existing_plot = PlotSpec(
        plot_id="plot-existing",
        kind="line",
        object_ids=(refs[0].object_id,),
    )
    project = Project(objects=refs, plots=(existing_plot,))
    handles = {ref.object_id: MagicMock() for ref in refs}
    fetched = {
        ref.object_id: _fetched(
            ref,
            handles[ref.object_id],
            shm_name=f"shm-{ref.object_id}",
        )
        for ref in refs
    }
    client = MagicMock()
    client.state.value = "running"

    def fetch_array(
        object_id: str, *, preview_stride: int | None = None
    ) -> FetchedArray:
        assert preview_stride == 2
        return fetched[object_id]

    client.fetch_array.side_effect = fetch_array
    session = MagicMock(spec=StudioSession)
    session.client = client
    return AlphaController(project=project, session=session), project, client, handles


def _assert_payload_api(payload: PreviewPayload) -> None:
    payload_type = type(payload)
    assert tuple(field.name for field in fields(payload_type)) == ("preview", "spec")
    assert payload_type.__slots__ == ("preview", "spec")
    assert not hasattr(payload, "__dict__")
    with pytest.raises(FrozenInstanceError):
        payload.spec = payload.spec  # type: ignore[misc]


def _assert_default_specs(payloads: tuple[PreviewPayload, ...]) -> None:
    timeseries, frequency, spectrogram = payloads
    assert timeseries.spec == PlotSpec(
        plot_id="plot-7",
        kind="line",
        object_ids=("obj-7",),
        xscale="linear",
        yscale="linear",
        xlim=None,
        ylim=None,
        title="H1:STRAIN",
        xlabel="Time [s]",
        ylabel="Amplitude [m]",
        legend=True,
        styles={"label": "H1:STRAIN"},
    )
    assert frequency.spec == PlotSpec(
        plot_id="plot-12",
        kind="line",
        object_ids=("obj-12",),
        xscale="linear",
        yscale="log",
        xlim=None,
        ylim=None,
        title="obj-12",
        xlabel="Frequency [Hz]",
        ylabel="Amplitude",
        legend=True,
        styles={"label": "obj-12"},
    )
    assert spectrogram.spec == PlotSpec(
        plot_id="plot-103",
        kind="spectrogram",
        object_ids=("obj-103",),
        xscale="linear",
        yscale="linear",
        xlim=None,
        ylim=None,
        title="Spectrogram A",
        xlabel="Time [s]",
        ylabel="Frequency [Hz]",
        legend=False,
        styles={"cmap": "viridis"},
    )


def _assert_complete_spec_rendering(preview: PreviewData) -> None:
    styles: dict[str, Any] = {
        "label": "custom trace",
        "color": "#123456",
        "linestyle": "--",
        "linewidth": 2.5,
    }
    original_styles = dict(styles)
    spec = PlotSpec(
        plot_id="plot-custom",
        kind="line",
        object_ids=(preview.ref.object_id,),
        xscale="log",
        yscale="log",
        xlim=(1.0, 4.0),
        ylim=(0.5, 5.0),
        title="Custom title",
        xlabel="Custom x",
        ylabel="Custom y",
        legend=True,
        styles=styles,
    )

    fig = render_preview(preview, spec)

    ax = fig.axes[0]
    line = ax.lines[0]
    assert ax.get_xscale() == "log"
    assert ax.get_yscale() == "log"
    assert ax.get_xlim() == pytest.approx((1.0, 4.0))
    assert ax.get_ylim() == pytest.approx((0.5, 5.0))
    assert ax.get_title() == "Custom title"
    assert ax.get_xlabel() == "Custom x"
    assert ax.get_ylabel() == "Custom y"
    assert [text.get_text() for text in ax.get_legend().get_texts()] == ["custom trace"]
    assert line.get_color() == "#123456"
    assert line.get_linestyle() == "--"
    assert line.get_linewidth() == 2.5
    assert np.array_equal(line.get_xdata(), np.array([1.0, 2.0, 3.0, 4.0]))
    assert styles == original_styles


@pytest.mark.contract("C-PLT-006")
def test_alpha_preview_payload_provides_complete_ephemeral_plot_spec() -> None:
    """The alpha payload owns one complete transient spec for exact rendering."""
    payload_type = getattr(application, "PreviewPayload", None)
    assert payload_type is not None, "application must export PreviewPayload"

    refs = (
        _ref("TimeSeries", 7, name="H1:STRAIN"),
        _ref("FrequencySeries", 12, name=None, unit=""),
        _ref("Spectrogram", 103, name="Spectrogram A"),
    )
    controller, project, client, handles = _controller_for(refs)
    plots_before = project.plots

    payloads = tuple(
        controller.fetch_preview_payload(ref.object_id, preview_stride=2)
        for ref in refs
    )

    assert all(isinstance(payload, payload_type) for payload in payloads)
    _assert_payload_api(payloads[0])
    _assert_default_specs(payloads)
    assert project.plots is plots_before
    assert client.fetch_array.call_args_list == [
        call("obj-7", preview_stride=2),
        call("obj-12", preview_stride=2),
        call("obj-103", preview_stride=2),
    ]
    assert client.release_array.call_args_list == [
        call("shm-obj-7"),
        call("shm-obj-12"),
        call("shm-obj-103"),
    ]
    for handle in handles.values():
        handle.close.assert_called_once_with()
    assert all(not payload.preview.values.flags.writeable for payload in payloads)

    direct_controller, _project, direct_client, direct_handles = _controller_for(
        (refs[0],)
    )
    direct_preview = direct_controller.fetch_preview("obj-7", preview_stride=2)
    assert isinstance(direct_preview, PreviewData)
    direct_client.fetch_array.assert_called_once_with("obj-7", preview_stride=2)
    direct_client.release_array.assert_called_once_with("shm-obj-7")
    direct_handles["obj-7"].close.assert_called_once_with()

    _assert_complete_spec_rendering(payloads[0].preview)
