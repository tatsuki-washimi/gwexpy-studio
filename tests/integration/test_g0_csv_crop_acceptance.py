"""G0 acceptance evidence for CSV read and crop through a real worker."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.support.g0_consistency import (
    assert_crop_worker_and_export_parity,
    assert_csv_worker_and_export,
)

pytestmark = pytest.mark.integration


@pytest.mark.contract("I-G0-001")
def test_g0_csv_read_matches_worker_fresh_export_and_public_oracle(
    tmp_path: Path,
) -> None:
    """Compare CSV values and time metadata across all three G0 paths."""
    assert_csv_worker_and_export(tmp_path)


@pytest.mark.contract("I-G0-002")
def test_g0_crop_forms_match_worker_fresh_export_and_public_oracle(
    hdf5_source: Path,
    timeseries: Any,
    tmp_path: Path,
) -> None:
    """Compare crop argument forms and out-of-range failure semantics."""
    assert_crop_worker_and_export_parity(hdf5_source, timeseries, tmp_path)
