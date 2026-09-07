"""Complex container analysis survives project reopening and standalone replay."""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import replace

import numpy as np
import pytest
from gwexpy.timeseries import TimeSeries, TimeSeriesDict

from gwexpy_studio.application.workspace_controller import WorkspaceController


@pytest.mark.contract("WSP-0104")
def test_complex_transfer_resume_preserves_units_coordinates_views_and_python(tmp_path):
    rng = np.random.default_rng(98)
    a = TimeSeries(rng.normal(size=4096), sample_rate=256, t0=10, unit="m", name="A")
    b = TimeSeries(3 * np.roll(a.value, 3), sample_rate=256, t0=10, unit="m", name="B")
    source = tmp_path / "日本語 container.h5"
    TimeSeriesDict({"入力": a, "出力": b}).write(source, format="hdf5")
    c = WorkspaceController(recovery=False)
    try:
        parent = c.read_io(
            c.inspect_io(
                {"paths": [str(source)], "datatype": "TimeSeriesDict", "format": "hdf5"}
            )
        )[0]
        handles = {
            "self": {"object_id": parent.object_id, "selector": {"key": "入力"}},
            "other": {"object_id": parent.object_id, "selector": {"key": "出力"}},
        }
        result = c.apply_multi(
            "timeseries.transfer_function", handles, {"fftlength": 1}
        )
        initial = c.fetch_member_preview_payload(result.object_id)
        assert np.max(np.abs(initial.preview.values.imag)) > 1
        spec = replace(
            initial.spec,
            xlim=(1.0, 100.0),
            ylim=(-10.0, 20.0),
            phase_ylim=(-180.0, 90.0),
            phase_unwrap=False,
        )
        c.set_plot_spec(spec)
        c.set_ui_state(
            {
                "selection": {"object_id": result.object_id, "selector": None},
                "panel_drafts": {},
            }
        )
        path = tmp_path / "complex.gwxproj"
        c.save_workspace(path)
        c.open_workspace(path)
        assert not c.workspace_status()["resident_object_ids"]
        c.restore_workspace(c.review_restore(), confirmed=True)
        restored = c.fetch_member_preview_payload(result.object_id)
        np.testing.assert_array_equal(restored.preview.values, initial.preview.values)
        np.testing.assert_array_equal(
            restored.preview.x_coordinates, initial.preview.x_coordinates
        )
        assert restored.preview.ref.unit == initial.preview.ref.unit
        assert restored.spec == spec
        c.undo_analysis()
        assert c.workspace_status()["active_object_ids"] == [parent.object_id]
        c.redo_analysis()
        script = tmp_path / "replay.py"
        c.export_script(script)
        var = result.object_id.replace("-", "_")
        script.write_text(
            script.read_text() + f"\nnp.savez('replayed.npz', values={var}.value, "
            f"coordinates={var}.frequencies.value, unit=str({var}.unit))\n",
            encoding="utf-8",
        )
        completed = subprocess.run(
            [sys.executable, str(script)],
            cwd=tmp_path,
            env={**os.environ, "MPLBACKEND": "Agg"},
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert completed.returncode == 0, completed.stderr
        with np.load(tmp_path / "replayed.npz") as exported:
            np.testing.assert_array_equal(exported["values"], initial.preview.values)
            np.testing.assert_array_equal(
                exported["coordinates"], initial.preview.x_coordinates
            )
            assert str(exported["unit"]) == initial.preview.ref.unit
    finally:
        c.finalize_workspace(clean=True)
