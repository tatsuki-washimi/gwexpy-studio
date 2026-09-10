"""Immutable trial seed verification contracts."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.release.test_trial_bundle import _write_trial_inputs

pytestmark = pytest.mark.unit


def _seed(tmp_path: Path) -> tuple[Path, str]:
    inputs = _write_trial_inputs(tmp_path)
    seed = tmp_path / "seed"
    seed.mkdir()
    for source_key, destination_name in (
        ("wheel", inputs["wheel"].name),
        ("source_manifest", "SOURCE-MANIFEST.json"),
        ("staging_manifest", "STAGING-MANIFEST.json"),
        ("trial_manifest", "TRIAL-MANIFEST.json"),
    ):
        (seed / destination_name).write_bytes(inputs[source_key].read_bytes())
    return seed, "abcdef0123456789abcdef0123456789abcdef01"


def test_seed_verifier_accepts_only_the_four_bound_build_outputs(
    tmp_path: Path,
) -> None:
    from scripts.verify_trial_seed import verify_trial_seed

    seed, source_sha = _seed(tmp_path)

    artifact = verify_trial_seed(seed, source_sha)

    assert artifact.source_sha == source_sha
    assert artifact.wheel_filename.endswith("-py3-none-any.whl")


@pytest.mark.parametrize("mutation", ["extra", "missing", "source"])
def test_seed_verifier_rejects_changed_assets_or_identity(
    tmp_path: Path, mutation: str
) -> None:
    from scripts.verify_trial_seed import TrialSeedError, verify_trial_seed

    seed, source_sha = _seed(tmp_path)
    if mutation == "extra":
        (seed / "extra.txt").write_text("extra\n", encoding="utf-8")
    elif mutation == "missing":
        (seed / "STAGING-MANIFEST.json").unlink()
    else:
        source_sha = "1" * 40

    with pytest.raises(TrialSeedError):
        verify_trial_seed(seed, source_sha)
