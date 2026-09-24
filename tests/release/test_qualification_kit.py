"""One-command physical qualification kit contracts."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.release.test_platform_trial_bundle import _assemble_platform
from tests.release.test_trial_bundle import _write_trial_inputs

pytestmark = pytest.mark.unit


def test_qualification_filename_is_target_architecture_and_build_bound() -> None:
    from scripts.run_platform_qualification import qualification_filename

    assert (
        qualification_filename("wsl2-ubuntu24", "x86_64", "P-abcdef0-20260907-r1-a1")
        == "qualification-wsl2-ubuntu24-x86_64-P-abcdef0-20260907-r1-a1.json"
    )


@pytest.mark.parametrize("target_id", ["wsl2-ubuntu24", "macos15-arm64"])
def test_kit_contains_candidate_pair_and_one_command_runner(
    tmp_path: Path, target_id: str
) -> None:
    from scripts.build_qualification_kit import build_qualification_kit
    from scripts.package_trial_release import package_trial_release

    inputs = _write_trial_inputs(tmp_path)
    bundle = _assemble_platform(inputs, tmp_path / "bundle", target_id)
    release = tmp_path / "release"
    archive, sidecar = package_trial_release(bundle, release)

    kit = build_qualification_kit(
        release_directory=release,
        output_directory=tmp_path / "kit",
        target_id=target_id,
    )

    names = {path.name for path in kit.iterdir()}
    assert {archive.name, sidecar.name, "run-qualification.sh"} <= names
    assert "run_platform_qualification.py" in names
    runner = (kit / "run-qualification.sh").read_text(encoding="utf-8")
    assert "conda create --prefix" in runner
    assert "python=3.12" in runner
    assert "run_platform_qualification.py" in runner
    assert (kit / "run-qualification.sh").stat().st_mode & 0o111


def test_kit_rejects_a_release_for_another_target(tmp_path: Path) -> None:
    from scripts.build_qualification_kit import (
        QualificationKitError,
        build_qualification_kit,
    )
    from scripts.package_trial_release import package_trial_release

    inputs = _write_trial_inputs(tmp_path)
    bundle = _assemble_platform(inputs, tmp_path / "bundle", "wsl2-ubuntu24")
    release = tmp_path / "release"
    package_trial_release(bundle, release)

    with pytest.raises(QualificationKitError, match="target"):
        build_qualification_kit(
            release_directory=release,
            output_directory=tmp_path / "kit",
            target_id="macos15-arm64",
        )


def test_kit_output_must_be_fresh_and_is_not_replaced(tmp_path: Path) -> None:
    from scripts.build_qualification_kit import (
        QualificationKitError,
        build_qualification_kit,
    )
    from scripts.package_trial_release import package_trial_release

    inputs = _write_trial_inputs(tmp_path)
    bundle = _assemble_platform(inputs, tmp_path / "bundle", "wsl2-ubuntu24")
    release = tmp_path / "release"
    package_trial_release(bundle, release)
    output = tmp_path / "kit"
    output.mkdir()
    marker = output / "marker"
    marker.write_text("keep", encoding="ascii")
    with pytest.raises(QualificationKitError, match="fresh"):
        build_qualification_kit(
            release_directory=release,
            output_directory=output,
            target_id="wsl2-ubuntu24",
        )
    assert marker.read_text(encoding="ascii") == "keep"


def test_standalone_runner_loads_verified_helpers_without_checkout_imports(
    tmp_path: Path,
) -> None:
    from scripts.build_qualification_kit import build_qualification_kit
    from scripts.package_trial_release import package_trial_release

    inputs = _write_trial_inputs(tmp_path)
    bundle = _assemble_platform(inputs, tmp_path / "bundle", "wsl2-ubuntu24")
    release = tmp_path / "release"
    package_trial_release(bundle, release)
    kit = build_qualification_kit(
        release_directory=release,
        output_directory=tmp_path / "kit",
        target_id="wsl2-ubuntu24",
    )
    fake_conda = tmp_path / "conda"
    fake_conda.write_text(
        f'#!/bin/sh\nshift 5\nexec "{sys.executable}" "$@"\n',
        encoding="ascii",
    )
    fake_conda.chmod(0o755)
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in {"PYTHONPATH", "PYTHONNOUSERSITE"}
    }
    environment["CONDA_EXE"] = str(fake_conda)
    completed = subprocess.run(
        [str(kit / "run-qualification.sh")],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        check=False,
    )
    assert completed.returncode != 0
    assert b"ModuleNotFoundError" not in completed.stderr
    result = kit / "results" / "qualification-untrusted.json"
    assert result.is_file(), completed.stderr.decode()
