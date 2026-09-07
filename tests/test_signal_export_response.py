import numpy as np
import pytest
from gwexpy.timeseries import TimeSeries


@pytest.mark.contract("SIG-0010")
def test_recorded_response_does_not_redesign_or_execute(monkeypatch):
    from gwexpy_studio.ops.native_filters import (
        filter_response,
        filter_response_from_recipes,
        science_filter,
    )

    series = TimeSeries(np.ones(256), sample_rate=128)
    result = science_filter("lowpass", {"self": series}, {"frequency": 15})
    expected = filter_response("lowpass", {"self": series}, {"frequency": 15})

    def forbidden(*args, **kwargs):
        raise AssertionError("Recorded response must not execute or redesign")

    monkeypatch.setattr(TimeSeries, "filter", forbidden)
    monkeypatch.setattr("gwexpy.signal.filter_design.lowpass", forbidden)
    response = filter_response_from_recipes(result.details)
    validated = filter_response(
        "lowpass", {"self": series}, {"frequency": 99}, recorded_details=result.details
    )
    np.testing.assert_array_equal(
        response["members"][0]["response"], expected["members"][0]["response"]
    )
    np.testing.assert_array_equal(
        validated["members"][0]["response"], expected["members"][0]["response"]
    )
