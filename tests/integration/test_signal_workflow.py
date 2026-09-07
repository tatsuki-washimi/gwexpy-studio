"""Real worker workflow from native container intake through standalone replay."""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import replace

import numpy as np
import pytest

from gwexpy_studio.application.alpha import AlphaController
from gwexpy_studio.domain.project import Project
from gwexpy_studio.errors import OperationError
from gwexpy_studio.ops.io import io_class, read_data, write_data


@pytest.mark.contract("SIG-0001")
def test_native_container_worker_analysis_write_and_export(tmp_path):
    cls = io_class("TimeSeries")
    rng = np.random.default_rng(4321)
    a = cls(rng.normal(size=4096), dt=1 / 256, t0=10, unit="m", name="Input A")
    b = cls(3 * a.value, dt=1 / 256, t0=10, unit="m", name="Output B")
    source = tmp_path / "入力 signals.h5"
    write_data(
        io_class("TimeSeriesDict")({"入力": a, "出力": b}), str(source), format="hdf5"
    )
    app = AlphaController()
    try:
        app.start()
        assert app.catalog_io("TimeSeriesDict", "read")["gwexpy_version"] == "0.2.0"
        request = {
            "datatype": "TimeSeriesDict",
            "paths": [str(source)],
            "format": "hdf5",
        }
        inspected = app.inspect_io(request)
        (parent,) = app.read_io(inspected)
        assert parent.kind == "TimeSeriesDict"
        members = app.list_members(parent.object_id)
        assert {m.selector["key"] for m in members} == {"入力", "出力"}
        before = len(app.project.graph.operations)
        preview = app.fetch_member_preview_payload(parent.object_id, {"key": "入力"})
        np.testing.assert_array_equal(preview.preview.values, a.value)
        np.testing.assert_array_equal(preview.preview.x_coordinates, a.times.value)
        assert len(app.project.graph.operations) == before
        handles = {
            "self": {"object_id": parent.object_id, "selector": {"key": "入力"}},
            "other": {"object_id": parent.object_id, "selector": {"key": "出力"}},
        }
        tf = app.apply_multi("timeseries.transfer_function", handles, {"fftlength": 1})
        tf_preview = app.fetch_member_preview_payload(tf.object_id)
        np.testing.assert_allclose(tf_preview.preview.values, 3, atol=1e-12)
        assert tf_preview.spec.kind == "bode"
        assert tf_preview.spec in app.project.plots
        untouched_script = tmp_path / "default_bode.py"
        app.export_script(untouched_script)
        assert "'kind': 'bode'" in untouched_script.read_text()
        app.set_plot_spec(replace(tf_preview.spec, phase_unwrap=False))
        assert app.fetch_member_preview_payload(tf.object_id).spec.phase_unwrap is False
        for operation in ("psd", "csd", "coherence"):
            selected = {"self": handles["self"]} if operation == "psd" else handles
            result = app.apply_multi(
                "timeseries." + operation, selected, {"fftlength": 1}
            )
            expected = getattr(a, operation)(
                *(() if operation == "psd" else (b,)), fftlength=1
            )
            np.testing.assert_allclose(
                app.fetch_member_preview_payload(result.object_id).preview.values,
                expected.value,
            )
        before = len(app.project.graph.operations)
        proposed = app.filter_preview(
            "timeseries.lowpass",
            {"self": handles["self"]},
            {"frequency": 30},
            generation=3,
        )
        assert proposed["generation"] == 3
        assert len(app.project.graph.operations) == before
        filtered = app.apply_multi(
            "timeseries.lowpass", {"self": handles["self"]}, {"frequency": 30}
        )
        record = app.project.executions[-1]
        assert record.details["filter_recipes"]
        recorded = app.filter_preview(
            "timeseries.lowpass",
            {"self": handles["self"]},
            {"frequency": 30},
            recorded_object_id=filtered.object_id,
        )
        np.testing.assert_array_equal(
            proposed["filter_preview"].preview.values,
            recorded["filter_preview"].preview.values,
        )
        doubled = app.apply_multi(
            "data.multiply",
            {"self": {"object_id": parent.object_id}},
            {"operand_mode": "scalar", "scalar": {"value": 2, "unit": "s"}},
        )
        assert doubled.kind == "TimeSeriesDict"
        target = tmp_path / "保存.h5"
        activity = app.write_data(
            doubled.object_id,
            {"datatype": "TimeSeriesDict", "paths": [str(target)], "format": "hdf5"},
        )
        assert activity.status == "succeeded"
        written = read_data("TimeSeriesDict", str(target), format="hdf5")
        np.testing.assert_allclose(written["入力"].value, 2 * a.value)
        assert str(written["入力"].unit) == "m s"
        manifest = app.project.to_dict()
        assert Project.from_dict(manifest).to_dict() == manifest
        script = tmp_path / "reproduce.py"
        app.export_script(script)
        assert "gwexpy_studio" not in script.read_text()
        environment = dict(os.environ)
        environment["MPLBACKEND"] = "Agg"
        output = subprocess.run(
            [sys.executable, str(script)],
            cwd=tmp_path,
            env=environment,
            text=True,
            capture_output=True,
            timeout=90,
        )
        assert output.returncode == 0, output.stderr
        # Native kind rejection happens before a new semantic operation is added.
        before = len(app.project.graph.operations)
        with pytest.raises(OperationError) as error:
            app.apply_multi("timeseries.psd", {"self": {"object_id": tf.object_id}}, {})
        assert error.value.code == "invalid_input_kind"
        assert len(app.project.graph.operations) == before
    finally:
        app.close()
    assert app.session.client.state.value == "CLOSED"
