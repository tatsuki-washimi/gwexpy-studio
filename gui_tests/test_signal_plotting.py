"""Display-only complex projections, coordinates, and Bode controls."""

from dataclasses import replace

import numpy as np
import pytest

from gwexpy_studio.application.preview import PreviewData
from gwexpy_studio.domain.model import DataObjectRef, PlotSpec
from gwexpy_studio.plotting.preview_renderer import render_preview


def _preview(values, *, kind="FrequencySeries", coordinates=None):
    values = np.asarray(values)
    axes = (
        {"f0": {"value": 0.0, "unit": "Hz"}, "df": {"value": 1.0, "unit": "Hz"}}
        if kind == "FrequencySeries"
        else {"t0": {"value": 0.0, "unit": "s"}, "dt": {"value": 1.0, "unit": "s"}}
    )
    ref = DataObjectRef(
        object_id="signal",
        kind=kind,
        shape=values.shape,
        dtype=str(values.dtype),
        unit="m",
        axes=axes,
    )
    kwargs = {} if coordinates is None else {"x_coordinates": coordinates}
    return PreviewData(ref=ref, values=values, unit="m", **kwargs)


@pytest.mark.gui
@pytest.mark.contract(id="GUI-SIG-PLT-001")
def test_bode_preserves_gaps_and_unwraps_each_valid_segment():
    """Mask only the displayed values and keep phase segments independent."""
    values = np.exp(1j * np.deg2rad([0, 170, -170, 0, -170, 170]))
    values[3] = complex(np.nan, np.nan)
    preview = _preview(values)
    before = preview.values.copy()
    spec = PlotSpec(plot_id="bode", kind="bode", object_ids=("signal",))
    fig = render_preview(preview, spec)
    assert len(fig.axes) == 2
    magnitude, phase = fig.axes
    assert magnitude.get_xscale() == phase.get_xscale() == "log"
    np.testing.assert_allclose(
        phase.lines[0].get_ydata(),
        [np.nan, 170, 190, np.nan, -170, -190],
        equal_nan=True,
    )
    assert "dB" in magnitude.get_ylabel() and "re" in magnitude.get_ylabel()
    np.testing.assert_array_equal(preview.values, before)


@pytest.mark.gui
@pytest.mark.contract(id="GUI-SIG-PLT-002")
def test_bode_reference_linear_and_wrapped_phase():
    """Honor reference amplitude, linear magnitude, and wrapped degrees."""
    preview = _preview(np.array([0, 2j, -2j], dtype=complex))
    spec = PlotSpec(
        plot_id="bode",
        kind="bode",
        object_ids=("signal",),
        db_reference=2.0,
        phase_unwrap=False,
    )
    fig = render_preview(preview, spec)
    np.testing.assert_allclose(
        fig.axes[0].lines[0].get_ydata(), [np.nan, 0, 0], equal_nan=True
    )
    np.testing.assert_allclose(
        fig.axes[1].lines[0].get_ydata(), [np.nan, 90, -90], equal_nan=True
    )
    fig = render_preview(preview, replace(spec, magnitude_scale="linear"))
    np.testing.assert_allclose(
        fig.axes[0].lines[0].get_ydata(), [np.nan, 2, 2], equal_nan=True
    )


@pytest.mark.gui
@pytest.mark.contract(id="GUI-SIG-PLT-003")
def test_complex_time_component_and_explicit_irregular_coordinates():
    """Draw an imaginary component at the original irregular coordinates."""
    preview = _preview(
        np.array([1 + 2j, 3 - 4j]),
        kind="TimeSeries",
        coordinates=np.array([10.0, 12.5]),
    )
    spec = PlotSpec(
        plot_id="line", kind="line", object_ids=("signal",), component="imag"
    )
    fig = render_preview(preview, spec)
    np.testing.assert_array_equal(fig.axes[0].lines[0].get_xdata(), [10.0, 12.5])
    np.testing.assert_array_equal(fig.axes[0].lines[0].get_ydata(), [2.0, -4.0])


@pytest.mark.contract(id="GUI-SIG-PLT-004")
@pytest.mark.gui
def test_canvas_controls_update_spec_without_modifying_data(qapp):
    """Emit persistent plot options from real controls while retaining data."""
    from gwexpy_studio.ui.plot_canvas import PlotCanvas

    preview = _preview(np.array([1 + 2j, 3 - 4j]))
    spec = PlotSpec(plot_id="bode", kind="bode", object_ids=("signal",))
    canvas = PlotCanvas()
    changes = []
    canvas.spec_changed.connect(changes.append)
    canvas.set_preview(preview, spec)
    canvas.magnitude_combo.setCurrentText("linear")
    canvas.phase_unwrap.setChecked(False)
    assert changes[-1].magnitude_scale == "linear"
    assert not changes[-1].phase_unwrap
    assert canvas._preview is preview
    import warnings

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        canvas.clear()
    assert not caught
    canvas.close()


@pytest.mark.gui
@pytest.mark.contract(id="GUI-SIG-PLT-005")
def test_export_renderer_matches_preview_bode_and_complex_components():
    """Use the same complex projections in desktop and exported figures."""
    from types import SimpleNamespace

    from gwexpy_studio.plotting.renderer import render_plot

    class Coordinates:
        def to_value(self, unit):
            return np.array([0.0, 1.0, 3.0])

    values = np.array([0.0, 1 + 1j, -1 - 1j])
    preview = _preview(values, coordinates=np.array([0.0, 1.0, 3.0]))
    obj = SimpleNamespace(value=values, frequencies=Coordinates(), unit="m")
    spec = PlotSpec(
        plot_id="bode",
        kind="bode",
        object_ids=("signal",),
        db_reference=2,
        phase_unwrap=False,
    )
    expected = render_preview(preview, spec)
    actual = render_plot(spec, {"signal": obj})
    assert len(actual.axes) == 2
    for actual_ax, expected_ax in zip(actual.axes, expected.axes, strict=True):
        np.testing.assert_allclose(
            actual_ax.lines[0].get_ydata(),
            expected_ax.lines[0].get_ydata(),
            equal_nan=True,
        )
    line = render_plot(replace(spec, kind="line", component="imag"), {"signal": obj})
    np.testing.assert_allclose(line.axes[0].lines[0].get_ydata(), [0, 1, -1])


@pytest.mark.gui
@pytest.mark.contract(id="GUI-SIG-PLT-006")
def test_invalid_display_reference_keeps_existing_figure():
    """Reject invalid display options before clearing the current plot."""
    preview = _preview(np.array([1 + 1j, 2 + 2j]))
    spec = PlotSpec(plot_id="bode", kind="bode", object_ids=("signal",))
    fig = render_preview(preview, spec)
    axes = tuple(fig.axes)
    with pytest.raises(ValueError, match="reference"):
        render_preview(preview, replace(spec, db_reference=0), fig=fig)
    assert tuple(fig.axes) == axes


@pytest.mark.gui
@pytest.mark.contract(id="GUI-SIG-PLT-007")
def test_complex_spectrogram_export_uses_irregular_coordinates():
    """Preserve irregular mesh coordinates and magnitude in exported plots."""
    from types import SimpleNamespace

    from gwexpy_studio.plotting.renderer import render_plot

    class Coordinates:
        def __init__(self, values):
            self.values = values

        def to_value(self, unit):
            return np.asarray(self.values)

    obj = SimpleNamespace(
        value=np.array([[1 + 1j, 2j], [3j, 4 + 4j]]),
        times=Coordinates([1.0, 3.0]),
        frequencies=Coordinates([2.0, 7.0]),
        unit="m",
    )
    spec = PlotSpec(
        plot_id="sg", kind="spectrogram", object_ids=("signal",), component="abs"
    )
    fig = render_plot(spec, {"signal": obj})
    assert len(fig.axes[0].collections) == 1
    np.testing.assert_allclose(
        fig.axes[0].collections[0].get_array(), [[np.sqrt(2), 3], [2, np.sqrt(32)]]
    )


@pytest.mark.gui
@pytest.mark.contract(id="GUI-SIG-PLT-008")
def test_display_unit_and_phase_update_axis_labels():
    """Label transformed values with their displayed physical quantity."""
    preview = _preview(np.array([1 + 1j, 2 + 2j]), kind="TimeSeries")
    spec = PlotSpec(
        plot_id="line",
        kind="line",
        object_ids=("signal",),
        ylabel="Amplitude [m]",
        display_unit="cm",
    )
    fig = render_preview(preview, spec)
    assert fig.axes[0].get_ylabel() == "Amplitude [cm]"
    fig = render_preview(preview, replace(spec, component="phase"))
    assert fig.axes[0].get_ylabel() == "Phase [deg]"


@pytest.mark.gui
@pytest.mark.contract(id="GUI-SIG-PLT-009")
def test_bode_export_stride_keeps_nan_boundaries():
    """Keep gaps and independent phase branches when reducing response samples."""
    from types import SimpleNamespace

    from gwexpy_studio.plotting.renderer import render_plot

    class Coordinates:
        def to_value(self, unit):
            return np.arange(1.0, 6.0)

    values = np.exp(1j * np.deg2rad([170, 0, -170, -150, -130]))
    values[1] = np.nan + 1j * np.nan
    obj = SimpleNamespace(value=values, frequencies=Coordinates(), unit="m")
    spec = PlotSpec(plot_id="bode", kind="bode", object_ids=("signal",))
    fig = render_plot(spec, {"signal": obj}, preview_stride=2)
    phases = fig.axes[1].lines[0].get_ydata()
    assert np.isnan(phases).any()
    assert phases[-1] == pytest.approx(-130)


@pytest.mark.gui
@pytest.mark.contract(id="GUI-SIG-PLT-010")
def test_renderer_selects_recorded_member_from_parent_collection(monkeypatch):
    """Draw the saved member selector while preserving the input object mapping."""
    from types import SimpleNamespace

    import gwexpy_studio.plotting.renderer as renderer

    class Coordinates:
        def to_value(self, unit):
            return np.array([1.0, 2.0])

    leaf = SimpleNamespace(
        value=np.array([1 + 1j, 2 + 2j]), frequencies=Coordinates(), unit="m"
    )
    parent = {"selected": leaf}
    calls = []

    def extract(value, selector):
        calls.append((value, selector))
        return value[selector["key"]]

    monkeypatch.setattr(renderer, "native_extract", extract, raising=False)
    spec = PlotSpec(
        plot_id="member",
        kind="bode",
        object_ids=("parent",),
        selectors={"parent": {"key": "selected"}},
    )
    objects = {"parent": parent}
    fig = renderer.render_plot(spec, objects)
    assert len(fig.axes) == 2
    assert calls == [(parent, {"key": "selected"})]
    assert objects["parent"] is parent


@pytest.mark.gui
@pytest.mark.contract(id="GUI-SIG-PLT-011")
def test_compact_response_labels_fit_without_overlap(qapp):
    """Keep response titles and axis labels readable in the modeless panel."""
    from gwexpy_studio.ui.plot_canvas import PlotCanvas

    canvas = PlotCanvas()
    canvas.resize(640, 460)
    canvas.show()
    preview = _preview(np.array([1 + 1j, 2 + 2j]))
    preview = replace(preview, ref=replace(preview.ref, unit=""), unit="")
    spec = PlotSpec(
        plot_id="response",
        kind="bode",
        object_ids=("signal",),
        title=(
            "Input A — Digital steady-state response: "
            "finite-record edge transients are excluded"
        ),
    )
    canvas.set_preview(preview, spec)
    qapp.processEvents()
    canvas.canvas.draw()
    renderer = canvas.canvas.get_renderer()
    magnitude, phase = canvas.figure.axes
    upper = magnitude.yaxis.label.get_window_extent(renderer)
    lower = phase.yaxis.label.get_window_extent(renderer)
    title = magnitude.title.get_window_extent(renderer)
    assert not upper.overlaps(lower)
    assert title.x0 >= 0 and title.x1 <= canvas.figure.bbox.x1
    assert canvas.component_combo.isHidden()
    assert canvas.db_reference.text() == "1"
    canvas.close()
