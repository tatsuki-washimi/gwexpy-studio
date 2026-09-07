"""Pure matplotlib Agg plot renderer implementation."""

from __future__ import annotations

import io
from collections.abc import Mapping
from typing import Any

import numpy as np
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure

from ..domain.model import PlotSpec
from ..ops.native_selection import native_extract
from .complex_display import component_values, display_values, display_ylabel, draw_bode

_KEPT_FONTS: list[Any] = []
try:
    import matplotlib.font_manager
    import matplotlib.ft2font

    for _font in matplotlib.font_manager.fontManager.ttflist:
        try:
            _KEPT_FONTS.append(matplotlib.ft2font.FT2Font(_font.fname))
        except Exception:
            pass
    for _family in ("sans-serif", "FreeSans", "DejaVu Sans", "Helvetica"):
        try:
            _fp = matplotlib.font_manager.FontProperties(family=_family)
            _file = matplotlib.font_manager.findfont(_fp)
            _KEPT_FONTS.append(matplotlib.ft2font.FT2Font(_file))
        except Exception:
            pass
    _warmup_fig = Figure()
    FigureCanvasAgg(_warmup_fig)
    _warmup_ax = _warmup_fig.subplots()
    _warmup_ax.plot([1.0, 10.0], [1.0, 10.0], label="warmup", color="black")
    _warmup_ax.set_xscale("log")
    _warmup_ax.set_yscale("log")
    _warmup_ax.set_title("warmup with $\\sqrt{f}$ [Hz]")
    _warmup_ax.set_xlabel("time [s]")
    _warmup_ax.set_ylabel("amplitude [m / Hz(1/2)]")
    _warmup_ax.legend()
    _warmup_fig.savefig(io.BytesIO(), format="png")
    _warmup_fig.clear()
except Exception:
    pass


def render_plot(
    plot_spec: PlotSpec,
    objects: Mapping[str, Any],
    *,
    preview_stride: int | None = None,
) -> Figure:
    """Render a configured PlotSpec onto an Agg Figure."""
    objects = {
        object_id: native_extract(obj, plot_spec.selectors[object_id])
        if object_id in plot_spec.selectors
        else obj
        for object_id, obj in objects.items()
    }
    fig = Figure()
    FigureCanvasAgg(fig)
    if plot_spec.kind == "bode":
        magnitude, phase = fig.subplots(2, 1, sharex=True)
        for object_id in plot_spec.object_ids:
            if object_id not in objects:
                continue
            obj = objects[object_id]
            x = np.asarray(obj.frequencies.to_value("Hz"))
            y = np.asarray(obj.value)
            draw_bode(
                magnitude,
                phase,
                x,
                y,
                plot_spec,
                unit=str(getattr(obj, "unit", "")),
                label=getattr(obj, "name", None) or object_id,
                preview_stride=preview_stride,
            )
        fig.tight_layout()
        return fig
    ax = fig.subplots()

    if plot_spec.kind == "line":
        for obj_id in plot_spec.object_ids:
            if obj_id not in objects:
                continue
            obj = objects[obj_id]
            if getattr(obj, "times", None) is not None:
                x = np.asarray(obj.times.to_value("s"))
            elif getattr(obj, "frequencies", None) is not None:
                x = np.asarray(obj.frequencies.to_value("Hz"))
            else:
                x = np.asarray(obj.coordinates)

            y = component_values(
                display_values(
                    np.asarray(obj.value), str(getattr(obj, "unit", "")), plot_spec
                ),
                plot_spec,
            )
            if preview_stride is not None and preview_stride > 1:
                x = x[::preview_stride]
                y = y[::preview_stride]

            plot_kwargs = dict(plot_spec.styles or {})
            label = (
                plot_kwargs.pop("label", None)
                or getattr(obj, "name", None)
                or getattr(obj, "channel", None)
                or obj_id
            )
            ax.plot(x, y, label=label, **plot_kwargs)

        if plot_spec.xscale:
            ax.set_xscale(plot_spec.xscale)
        if plot_spec.yscale:
            ax.set_yscale(plot_spec.yscale)
        if plot_spec.xlim:
            ax.set_xlim(*plot_spec.xlim)
        if plot_spec.ylim:
            ax.set_ylim(*plot_spec.ylim)
        if plot_spec.title:
            ax.set_title(plot_spec.title)
        if plot_spec.xlabel:
            ax.set_xlabel(plot_spec.xlabel)
        ylabel = display_ylabel(plot_spec)
        if ylabel:
            ax.set_ylabel(ylabel)
        if plot_spec.legend:
            ax.legend()

    elif plot_spec.kind == "spectrogram":
        for obj_id in plot_spec.object_ids:
            if obj_id not in objects:
                continue
            obj = objects[obj_id]
            data = component_values(
                display_values(
                    np.asarray(obj.value), str(getattr(obj, "unit", "")), plot_spec
                ),
                plot_spec,
            )

            extent: tuple[float, float, float, float] | None = None
            if (
                getattr(obj, "times", None) is not None
                and getattr(obj, "frequencies", None) is not None
            ):
                times = np.asarray(obj.times.to_value("s"))
                frequencies = np.asarray(obj.frequencies.to_value("Hz"))
                if getattr(obj, "dt", None) is not None:
                    dt_val = float(obj.dt.to_value("s"))
                else:
                    dt_val = times[1] - times[0] if len(times) > 1 else 1.0

                if getattr(obj, "df", None) is not None:
                    df_val = float(obj.df.to_value("Hz"))
                else:
                    df_val = (
                        frequencies[1] - frequencies[0] if len(frequencies) > 1 else 1.0
                    )

                # Format is (left, right, bottom, top)
                if type(obj).__name__ == "Spectrogram":
                    extent = (
                        float(times[0]),
                        float(times[-1] + dt_val),
                        float(frequencies[0] - df_val / 2.0),
                        float(frequencies[-1] + df_val / 2.0),
                    )
                else:
                    extent = (
                        float(times[0]),
                        float(times[-1] + dt_val),
                        float(frequencies[0]),
                        float(frequencies[-1] + df_val),
                    )

            cmap_name = (
                plot_spec.styles.get("cmap", "viridis")
                if plot_spec.styles
                else "viridis"
            )

            complex_or_irregular = np.iscomplexobj(obj.value) or (
                extent is not None
                and (
                    (
                        len(times) > 2
                        and not np.allclose(np.diff(times), np.diff(times)[0])
                    )
                    or (
                        len(frequencies) > 2
                        and not np.allclose(
                            np.diff(frequencies), np.diff(frequencies)[0]
                        )
                    )
                )
            )
            if complex_or_irregular and extent is not None:
                mesh = ax.pcolormesh(
                    times, frequencies, data.T, shading="auto", cmap=cmap_name
                )
                colorbar = fig.colorbar(mesh, ax=ax)
                colorbar.set_label(
                    "deg"
                    if plot_spec.component == "phase"
                    else (plot_spec.display_unit or str(getattr(obj, "unit", "")))
                )
            else:
                ax.imshow(
                    data.T,
                    origin="lower",
                    aspect="auto",
                    extent=extent,
                    cmap=cmap_name,
                )

            if plot_spec.xscale:
                ax.set_xscale(plot_spec.xscale)
            if plot_spec.yscale:
                ax.set_yscale(plot_spec.yscale)
            if plot_spec.xlim:
                ax.set_xlim(*plot_spec.xlim)
            if plot_spec.ylim:
                ax.set_ylim(*plot_spec.ylim)
            elif plot_spec.yscale == "log" and extent is not None:
                # 対数軸では非正の辺を表示できない。f0 > df/2 なら真の下端、
                # f0 == 0(DC 込み)なら DC ビンの上端 df/2 を下限にする。
                # 上流 gwpy は下限に df(ビン1の中心)を使うが、ビンが
                # f ± df/2 に広がる以上、表示可能な最小の正の辺は df/2 で
                # あるため意図的に異なる値を採る。
                bottom_f = extent[2] if extent[2] > 0.0 else df_val / 2.0
                top_f = extent[3]
                ax.set_ylim(bottom=bottom_f, top=top_f)

            if plot_spec.title:
                ax.set_title(plot_spec.title)
            if plot_spec.xlabel:
                ax.set_xlabel(plot_spec.xlabel)
            if plot_spec.ylabel:
                ax.set_ylabel(plot_spec.ylabel)

    return fig
