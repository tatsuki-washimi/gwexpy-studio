"""Fresh-process numerical oracle for cropped native sampling metadata."""

import os
import subprocess
import sys

import numpy as np
import pytest
from gwexpy.timeseries import TimeSeries

from gwexpy_studio.domain.graph import OperationGraph
from gwexpy_studio.domain.model import DataObjectRef, Operation
from gwexpy_studio.domain.project import Project
from gwexpy_studio.export import export_python
from gwexpy_studio.ops.registry import REGISTRY


@pytest.mark.contract("SIG-0065")
def test_cropped_psd_export_keeps_native_cadence_and_fft_bins(tmp_path):
    source = TimeSeries(
        np.random.default_rng(53).normal(size=1000),
        times=np.arange(1000) * 0.001,
        unit="m",
        name="sample",
    )
    path = tmp_path / "source.h5"
    source.write(path, format="hdf5")
    expected = (
        TimeSeries.read(path, format="hdf5")
        .crop(0.1, 0.9)
        .detrend("linear")
        .psd(fftlength=0.1, overlap=0.0)
    )
    operations = []
    objects = []
    previous = None
    actual = None
    for index, (name, params) in enumerate(
        (
            (
                "data.read",
                {"datatype": "TimeSeries", "source": str(path), "format": "hdf5"},
            ),
            ("timeseries.crop", {"start": 0.1, "end": 0.9}),
            ("timeseries.detrend", {"detrend": "linear"}),
            ("timeseries.psd", {"fftlength": 0.1, "overlap": 0.0}),
        )
    ):
        operation_id = f"op-{index}"
        object_id = f"obj-{index}"
        inputs = {} if previous is None else {"self": previous}
        actual = REGISTRY[name].apply(
            {} if actual is None else {"self": actual}, params
        )
        operations.append(
            Operation(
                op_id=operation_id,
                operation_id=name,
                operation_schema=1,
                inputs=inputs,
                params=params,
                outputs=(object_id,),
            )
        )
        objects.append(
            DataObjectRef(
                object_id=object_id,
                kind=type(actual).__name__,
                shape=actual.shape,
                dtype=str(actual.dtype),
                unit=str(actual.unit),
                produced_by=operation_id,
            )
        )
        previous = object_id
    assert actual.shape == expected.shape == (51,)
    np.testing.assert_array_equal(actual.frequencies, expected.frequencies)
    np.testing.assert_allclose(actual.value, expected.value)
    project = Project(objects=tuple(objects), graph=OperationGraph(operations))
    output = tmp_path / "psd.npy"
    coordinates = tmp_path / "frequency.npy"
    script = tmp_path / "standalone.py"
    generated = export_python(project, value_dumps={"obj-3": output})
    assert "def science_copy(" in generated
    assert "gwexpy_studio" not in generated
    script.write_text(
        generated + f"\nnp.save({str(coordinates)!r}, obj_3.frequencies.value)\n",
        encoding="utf-8",
    )
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    completed = subprocess.run(
        [sys.executable, str(script)],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    np.testing.assert_array_equal(np.load(coordinates), expected.frequencies.value)
    np.testing.assert_allclose(np.load(output), expected.value)
