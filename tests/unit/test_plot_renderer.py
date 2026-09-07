"""Contract tests for the Agg Matplotlib PlotSpec renderer."""

from __future__ import annotations

from typing import Any

import matplotlib
import numpy as np
import pytest
from matplotlib.figure import Figure

matplotlib.use("Agg", force=True)

from gwexpy_studio.domain.model import PlotSpec
from gwexpy_studio.plotting.renderer import render_plot


@pytest.mark.contract("B-043")
def test_line_plot_uses_object_values_labels_and_styles(timeseries: Any) -> None:
    """Line rendering uses the declared object, labels, scales, and style."""
    plot_spec = PlotSpec(
        plot_id="plot-1",
        kind="line",
        object_ids=("obj-1",),
        xscale="linear",
        yscale="linear",
        title="Time series",
        xlabel="GPS seconds",
        ylabel="Strain [m]",
        legend=True,
        styles={"color": "tab:blue", "linewidth": 2.0, "label": "X1"},
    )

    figure = render_plot(plot_spec, {"obj-1": timeseries})

    assert isinstance(figure, Figure)
    axes = figure.axes[0]
    assert len(axes.lines) == 1
    assert axes.get_xscale() == "linear"
    assert axes.get_yscale() == "linear"
    assert axes.get_title() == "Time series"
    assert axes.get_xlabel() == "GPS seconds"
    assert axes.get_ylabel() == "Strain [m]"
    line = axes.lines[0]
    source_x = np.asarray(timeseries.times.to_value("s"))
    source_y = np.asarray(timeseries.value)
    assert np.isfinite(source_x).all()
    assert np.isfinite(source_y).all()
    rendered_x = np.asarray(line.get_xdata())
    rendered_y = np.asarray(line.get_ydata())
    np.testing.assert_allclose(rendered_x, source_x, rtol=1e-12, atol=1e-12)
    np.testing.assert_allclose(rendered_y, source_y, rtol=1e-12, atol=1e-12)
    assert line.get_color() == "tab:blue"
    assert line.get_linewidth() == 2.0
    legend = axes.get_legend()
    assert legend is not None
    assert [text.get_text() for text in legend.get_texts()] == ["X1"]


@pytest.mark.contract("B-044")
def test_asd_plot_uses_frequency_axis_and_nonnegative_values(
    sine_32hz: Any,
) -> None:
    """ASD rendering derives frequency coordinates from f0 and df."""
    asd = sine_32hz.asd(fftlength=4.0)
    plot_spec = PlotSpec(
        plot_id="plot-2",
        kind="line",
        object_ids=("obj-asd",),
        xlabel="Frequency [Hz]",
        ylabel="ASD [m / sqrt(Hz)]",
        styles={"color": "black"},
    )

    figure = render_plot(plot_spec, {"obj-asd": asd})

    axes = figure.axes[0]
    line = axes.lines[0]
    frequencies = np.asarray(asd.frequencies.to_value("Hz"))
    values = np.asarray(asd.value)
    rendered_x = np.asarray(line.get_xdata())
    rendered_y = np.asarray(line.get_ydata())
    assert np.isfinite(frequencies).all()
    assert np.isfinite(values).all()
    np.testing.assert_allclose(rendered_x, frequencies, rtol=1e-12, atol=1e-12)
    np.testing.assert_allclose(rendered_y, values, rtol=1e-12, atol=1e-12)
    assert (rendered_y >= 0.0).all()
    assert frequencies[np.argmax(rendered_y)] == pytest.approx(32.0, abs=1e-12)
    assert axes.get_xlabel() == "Frequency [Hz]"


@pytest.mark.contract("B-045")
def test_spectrogram_plot_preserves_orientation_extent_scales_and_labels(
    sine_32hz: Any,
) -> None:
    """Spectrogram rendering maps time to x and frequency to y with transpose."""
    import gwexpy

    gwexpy.register_all(include_io=False)
    spectrogram = sine_32hz.spectrogram(stride=4.0, fftlength=2.0)
    plot_spec = PlotSpec(
        plot_id="plot-3",
        kind="spectrogram",
        object_ids=("obj-spec",),
        xscale="linear",
        yscale="log",
        title="Spectrogram",
        xlabel="Time [s]",
        ylabel="Frequency [Hz]",
        styles={"cmap": "viridis"},
    )

    figure = render_plot(plot_spec, {"obj-spec": spectrogram})

    axes = figure.axes[0]
    image = axes.images[0]
    times = np.asarray(spectrogram.times.to_value("s"))
    frequencies = np.asarray(spectrogram.frequencies.to_value("Hz"))
    values = np.asarray(spectrogram.value)
    dt = float(spectrogram.dt.to_value("s"))
    df = float(spectrogram.df.to_value("Hz"))
    expected_extent = (
        times[0],
        times[-1] + dt,
        frequencies[0] - df / 2.0,
        frequencies[-1] + df / 2.0,
    )
    assert np.isfinite(values).all()
    expected_values = values.T
    assert values.shape == (times.size, frequencies.size)
    assert expected_values.shape == (frequencies.size, times.size)
    rendered_values = np.asarray(image.get_array())
    assert np.isfinite(rendered_values).all()
    np.testing.assert_allclose(rendered_values, expected_values, rtol=1e-12, atol=1e-12)
    assert rendered_values.shape == (frequencies.size, times.size)
    assert image.origin == "lower"
    assert image.get_cmap().name == "viridis"
    assert np.allclose(image.get_extent(), expected_extent, rtol=1e-12, atol=1e-12)
    assert axes.get_xscale() == "linear"
    assert axes.get_yscale() == "log"
    ymin, ymax = axes.get_ylim()
    # 下限はデータ由来でなければならない(ハードコード床の禁止)
    assert ymin == pytest.approx(df / 2.0, rel=1e-12), (
        f"log-scale lower limit must be the DC-bin upper edge "
        f"df/2={df / 2.0}, got {ymin}"
    )
    assert ymax == pytest.approx(frequencies[-1] + df / 2.0, rel=1e-12)
    # 軸範囲がデータの周波数範囲(表示可能な正の部分)を包含する
    assert ymin <= frequencies[1] <= ymax
    assert ymin <= frequencies[-1] <= ymax
    assert axes.get_title() == "Spectrogram"
    assert axes.get_xlabel() == "Time [s]"
    assert axes.get_ylabel() == "Frequency [Hz]"


@pytest.mark.contract("B-082")
def test_spectrogram_known_frequency_row_lands_at_its_axis_position(
    sine_32hz: Any,
) -> None:
    """The 32 Hz excitation occupies the row whose axis coordinate is 32 Hz."""
    import gwexpy

    gwexpy.register_all(include_io=False)
    spectrogram = sine_32hz.spectrogram(stride=4.0, fftlength=2.0)
    plot_spec = PlotSpec(
        plot_id="plot-3f",
        kind="spectrogram",
        object_ids=("obj-spec",),
        xscale="linear",
        yscale="log",
    )

    figure = render_plot(plot_spec, {"obj-spec": spectrogram})
    image = figure.axes[0].images[0]
    rendered = np.asarray(image.get_array())
    frequencies = np.asarray(spectrogram.frequencies.to_value("Hz"))
    df = float(spectrogram.df.to_value("Hz"))
    extent = image.get_extent()

    peak_row = int(np.argmax(rendered.mean(axis=1)))
    assert abs(frequencies[peak_row] - 32.0) <= df
    row_center = extent[2] + (peak_row + 0.5) * df
    assert abs(row_center - 32.0) <= df / 2.0
    ymin, ymax = figure.axes[0].get_ylim()
    assert ymin < 32.0 < ymax


@pytest.mark.contract("B-046")
def test_preview_stride_does_not_mutate_scientific_input(timeseries: Any) -> None:
    """Preview subsampling renders [::n] and leaves source values untouched."""
    original = np.array(timeseries.value, copy=True)
    source_x = np.asarray(timeseries.times.to_value("s"))
    plot_spec = PlotSpec(
        plot_id="plot-4",
        kind="line",
        object_ids=("obj-1",),
    )

    figure = render_plot(plot_spec, {"obj-1": timeseries}, preview_stride=8)
    line = figure.axes[0].lines[0]
    expected_x = source_x[::8]
    expected_y = original[::8]
    assert len(line.get_xdata()) == len(expected_x)
    assert len(line.get_ydata()) == len(expected_y)
    np.testing.assert_allclose(line.get_xdata(), expected_x, rtol=1e-12, atol=1e-12)
    np.testing.assert_allclose(line.get_ydata(), expected_y, rtol=1e-12, atol=1e-12)

    assert np.array_equal(timeseries.value, original)


@pytest.mark.contract("B-047")
def test_renderer_returns_figure_without_persisting_one_in_plotspec(
    timeseries: Any,
) -> None:
    """PlotSpec remains a data declaration while render_plot returns a Figure."""
    plot_spec = PlotSpec(
        plot_id="plot-5",
        kind="line",
        object_ids=("obj-1",),
        styles={"color": "tab:orange"},
    )
    styles_before = dict(plot_spec.styles)

    figure = render_plot(plot_spec, {"obj-1": timeseries})

    assert isinstance(figure, Figure)
    assert dict(plot_spec.styles) == styles_before
    assert not hasattr(plot_spec, "figure")
