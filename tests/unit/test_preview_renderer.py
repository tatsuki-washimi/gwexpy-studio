"""Unit tests for pure Matplotlib preview renderer."""

from __future__ import annotations

import numpy as np
import pytest
from matplotlib.figure import Figure

from gwexpy_studio.application.preview import PreviewData
from gwexpy_studio.domain.model import DataObjectRef, ObjectKind, PlotKind, PlotSpec
from gwexpy_studio.plotting.preview_renderer import render_preview


def _make_ref(
    *,
    object_id: str = "obj-1",
    kind: ObjectKind = "TimeSeries",
    shape: tuple[int, ...] = (100,),
    dtype: str = "float64",
    unit: str = "m",
    axes: dict | None = None,
) -> DataObjectRef:
    default_axes = {
        "t0": {"value": 0.0, "unit": "s"},
        "dt": {"value": 0.01, "unit": "s"},
    }
    return DataObjectRef(
        object_id=object_id,
        kind=kind,
        shape=shape,
        dtype=dtype,
        unit=unit,
        axes=default_axes if axes is None else axes,
    )


def _make_spec(
    preview: PreviewData,
    *,
    kind: PlotKind = "line",
    xscale: str = "linear",
    yscale: str = "linear",
    xlabel: str | None = "Time [s]",
    ylabel: str | None = "Amplitude [m]",
    legend: bool = True,
    styles: dict | None = None,
) -> PlotSpec:
    return PlotSpec(
        plot_id="plot-1",
        kind=kind,
        object_ids=(preview.ref.object_id,),
        xscale=xscale,
        yscale=yscale,
        title=preview.ref.name or preview.ref.object_id,
        xlabel=xlabel,
        ylabel=ylabel,
        legend=legend,
        styles={"label": preview.ref.object_id} if styles is None else styles,
    )


def _spectrogram_preview() -> PreviewData:
    axes = {
        "t0": {"value": 0.0, "unit": "s"},
        "dt": {"value": 0.1, "unit": "s"},
        "f0": {"value": 10.0, "unit": "Hz"},
        "df": {"value": 1.0, "unit": "Hz"},
    }
    ref = _make_ref(kind="Spectrogram", shape=(20, 30), axes=axes)
    values = np.random.default_rng(0).random((20, 30))
    return PreviewData(ref=ref, values=values, unit="m")


def _frequency_preview() -> PreviewData:
    axes = {
        "f0": {"value": 1.0, "unit": "Hz"},
        "df": {"value": 0.5, "unit": "Hz"},
    }
    ref = _make_ref(object_id="obj-frequency", kind="FrequencySeries", axes=axes)
    return PreviewData(ref=ref, values=np.ones(100, dtype=np.float64), unit="m")


def _unsupported_preview() -> PreviewData:
    ref = _make_ref()
    object.__setattr__(ref, "kind", "Unknown")
    preview = PreviewData.__new__(PreviewData)
    object.__setattr__(preview, "ref", ref)
    object.__setattr__(preview, "values", np.zeros(100))
    object.__setattr__(preview, "unit", "m")
    return preview


def _assert_rejected_without_figure_mutation(
    preview: object,
    spec: object,
    expected_error: type[BaseException],
    *,
    match: str | None = None,
) -> None:
    fig = Figure()
    ax = fig.subplots()
    line = ax.plot([1.0, 2.0], [3.0, 4.0], label="sentinel")[0]
    ax.set_title("sentinel title")
    axes_before = tuple(fig.axes)
    xdata_before = np.asarray(line.get_xdata()).copy()
    ydata_before = np.asarray(line.get_ydata()).copy()

    with pytest.raises(expected_error, match=match):
        render_preview(preview, spec, fig=fig)  # type: ignore[arg-type]

    assert tuple(fig.axes) == axes_before
    assert fig.axes[0] is ax
    assert ax.get_title() == "sentinel title"
    assert len(ax.lines) == 1
    assert ax.lines[0] is line
    assert np.array_equal(line.get_xdata(), xdata_before)
    assert np.array_equal(line.get_ydata(), ydata_before)


class TestPreviewRenderer:
    """Validate Matplotlib Figure rendering from PreviewData."""

    @pytest.mark.contract("C-PLT-001")
    def test_render_timeseries_preview(self) -> None:
        """TimeSeries preview produces Figure with line plot and correct axis labels."""
        ref = _make_ref()
        values = np.sin(np.linspace(0, 10, 100))
        preview = PreviewData(ref=ref, values=values, unit="m")
        spec = _make_spec(preview)

        fig = render_preview(preview, spec)
        assert isinstance(fig, Figure)
        ax = fig.axes[0]
        assert ax.get_xlabel() == "Time [s]"
        assert "Amplitude [m]" in ax.get_ylabel()
        assert len(ax.lines) == 1

    @pytest.mark.contract("C-PLT-002")
    def test_render_frequencyseries_preview(self) -> None:
        """FrequencySeries preview uses approved linear/log alpha defaults."""
        axes = {
            "f0": {"value": 1.0, "unit": "Hz"},
            "df": {"value": 0.5, "unit": "Hz"},
        }
        ref = _make_ref(kind="FrequencySeries", unit="1/Hz(1/2)", axes=axes)
        values = np.exp(-np.linspace(0, 5, 100)) + 1e-10
        preview = PreviewData(ref=ref, values=values, unit="1/Hz(1/2)")
        spec = _make_spec(
            preview,
            xscale="linear",
            yscale="log",
            xlabel="Frequency [Hz]",
            ylabel="Amplitude [1/Hz(1/2)]",
        )

        fig = render_preview(preview, spec)
        assert isinstance(fig, Figure)
        ax = fig.axes[0]
        assert ax.get_xlabel() == "Frequency [Hz]"
        assert "Amplitude" in ax.get_ylabel()
        assert ax.get_xscale() == "linear"
        assert ax.get_yscale() == "log"

    @pytest.mark.contract("C-PLT-003")
    def test_render_spectrogram_preview(self) -> None:
        """Spectrogram preview produces Figure with pcolormesh and colorbar."""
        preview = _spectrogram_preview()
        spec = _make_spec(
            preview,
            kind="spectrogram",
            xlabel="Time [s]",
            ylabel="Frequency [Hz]",
            legend=False,
            styles={"cmap": "viridis"},
        )

        fig = render_preview(preview, spec)
        assert isinstance(fig, Figure)
        assert len(fig.axes) == 2  # main plot + colorbar
        ax = fig.axes[0]
        assert ax.get_xlabel() == "Time [s]"
        assert ax.get_ylabel() == "Frequency [Hz]"

    @pytest.mark.contract("C-PLT-004")
    def test_render_rejects_non_preview_data(self) -> None:
        """Passing raw objects or dictionaries raises TypeError."""
        spec = PlotSpec(plot_id="plot-1", kind="line", object_ids=("obj-1",))
        _assert_rejected_without_figure_mutation(
            "not a preview", spec, TypeError, match="Expected PreviewData"
        )

    @pytest.mark.contract("C-PLT-005")
    def test_render_rejects_unsupported_kind(self) -> None:
        """Unsupported kind raises ValueError."""
        preview = _unsupported_preview()
        spec = PlotSpec(plot_id="plot-1", kind="line", object_ids=("obj-1",))
        _assert_rejected_without_figure_mutation(
            preview, spec, ValueError, match="Unsupported preview kind"
        )

    @pytest.mark.contract("C-PLT-007")
    def test_render_rejects_incoherent_specs_before_figure_mutation(self) -> None:
        """Missing or mismatched specs fail before a caller Figure is cleared."""
        preview = PreviewData(
            ref=_make_ref(), values=np.ones(100, dtype=np.float64), unit="m"
        )
        valid_spec = _make_spec(preview)
        frequency = _frequency_preview()
        spectrogram = _spectrogram_preview()
        cases: tuple[tuple[object, object, type[BaseException]], ...] = (
            (preview, None, TypeError),
            (preview, object(), TypeError),
            (
                preview,
                PlotSpec(plot_id="plot-1", kind="line", object_ids=()),
                ValueError,
            ),
            (
                preview,
                PlotSpec(plot_id="plot-1", kind="line", object_ids=("obj-other",)),
                ValueError,
            ),
            (
                preview,
                PlotSpec(
                    plot_id="plot-1",
                    kind="line",
                    object_ids=(preview.ref.object_id, "obj-other"),
                ),
                ValueError,
            ),
            (
                preview,
                PlotSpec(
                    plot_id="plot-1",
                    kind="spectrogram",
                    object_ids=(preview.ref.object_id,),
                ),
                ValueError,
            ),
            (
                frequency,
                PlotSpec(
                    plot_id="plot-frequency",
                    kind="spectrogram",
                    object_ids=(frequency.ref.object_id,),
                ),
                ValueError,
            ),
            (
                spectrogram,
                PlotSpec(
                    plot_id="plot-1",
                    kind="line",
                    object_ids=(spectrogram.ref.object_id,),
                ),
                ValueError,
            ),
        )

        for candidate_preview, candidate_spec, expected_error in cases:
            _assert_rejected_without_figure_mutation(
                candidate_preview, candidate_spec, expected_error
            )

        assert valid_spec.object_ids == (preview.ref.object_id,)
