"""Real-file checks for the reproducible native versus Studio I/O evidence tool."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "verify_signal_io.py"


@pytest.fixture(scope="module")
def matrix():
    spec = importlib.util.spec_from_file_location("verify_signal_io", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("family", ["TimeSeries", "FrequencySeries", "Spectrogram"])
@pytest.mark.parametrize("suffix", ["", "Dict", "List", "Matrix"])
def test_hdf5_direct_and_adapter_share_real_file_and_preserve_native_results(
    matrix, tmp_path, family, suffix
):
    name = family + suffix
    case = matrix.run_case(name, "hdf5", tmp_path, tmp_path / "missing")
    assert case["write"]["status"] == "equivalent_success", case
    assert case["read"]["status"] == "equivalent_success", case
    assert case["read"]["direct"]["path"] == case["read"]["adapter"]["path"]
    assert case["read"]["direct"]["result"]["class"] == name
    assert case["source_unchanged"] is True


@pytest.mark.contract("SIG-0083")
def test_missing_readonly_fixture_is_not_a_success(matrix, tmp_path):
    case = matrix.run_case(
        "TimeSeries", "li", tmp_path, tmp_path / "missing", write=False
    )
    assert case["read"]["status"] == "fixture_missing"
    assert case["read"]["direct"]["attempted"] is False
    assert case["read"]["adapter"]["attempted"] is False


@pytest.mark.contract("SIG-0084")
def test_native_unimplemented_io_is_not_counted_as_equivalent_success(matrix, tmp_path):
    case = matrix.run_case(
        "Spectrogram", "not_a_native_format", tmp_path, tmp_path / "missing"
    )
    assert case["write"]["direct"]["attempted"] is True
    assert case["write"]["status"] == "native_unimplemented"
    assert case["write"]["adapter_matches_native"] is True


@pytest.mark.contract("SIG-0085")
def test_inventory_includes_all_classes_and_formats_outside_registry(matrix):
    inventory = matrix.inventory()
    assert len(inventory) == 12
    assert "hdf5" in inventory["TimeSeriesList"]["direct_extra_read"]
    assert "hdf5" in inventory["TimeSeriesList"]["direct_extra_write"]
    assert all(item["read"] or item["direct_extra_read"] for item in inventory.values())
