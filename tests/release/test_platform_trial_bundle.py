"""Schema-4 platform trial bundle contracts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.release.test_trial_bundle import (
    _canonical_json,
    _refresh_checksums,
    _sha256,
    _write_trial_inputs,
)

pytestmark = pytest.mark.unit


def _module():
    from scripts import assemble_trial_bundle

    return assemble_trial_bundle


def _assemble_platform(inputs: dict[str, Path], output: Path, target_id: str) -> Path:
    if target_id == "wsl2-ubuntu24":
        evidence = {
            architecture: (
                inputs[f"constraints_{architecture}"],
                inputs[f"resolution_{architecture}"],
            )
            for architecture in ("aarch64", "x86_64")
        }
    else:
        evidence = {"arm64": _macos_evidence(inputs)}
    return _module().assemble_platform_trial_bundle(
        output_directory=output,
        wheel=inputs["wheel"],
        preliminary_trial_manifest=inputs["trial_manifest"],
        source_manifest=inputs["source_manifest"],
        staging_manifest=inputs["staging_manifest"],
        architecture_evidence=evidence,
        quick_start=inputs["quick_start"],
        quick_start_ja=inputs["quick_start_ja"],
        feedback_ja=inputs["feedback"],
        trial_target_id=target_id,
        repository="example/gwexpy-studio",
        build_workflow_path=".github/workflows/build-trial-wheel.yml",
        build_run_id=123456,
        build_run_number=1,
        build_run_attempt=1,
    )


def _macos_evidence(inputs: dict[str, Path]) -> tuple[Path, Path]:
    constraints = inputs["constraints_aarch64"].with_name(
        "constraints-macos15-arm64.txt"
    )
    constraints.write_bytes(inputs["constraints_aarch64"].read_bytes())
    linux_resolution = json.loads(inputs["resolution_aarch64"].read_bytes())
    linux_resolution["architecture"] = "arm64"
    linux_resolution["constraints_sha256"] = _sha256(constraints.read_bytes())
    linux_resolution["schema"] = 3
    linux_resolution["technical_gate"]["architecture"] = "arm64"
    for field in ("glibc_version", "os_id", "os_runtime", "os_version"):
        del linux_resolution[field]
    linux_resolution["platform"] = {
        "architecture": "arm64",
        "id": "macos",
        "qt_opengl_context": True,
        "qt_platform": "cocoa",
        "qt_version": "6.11.2",
        "version": "15.7.1",
    }
    resolution = constraints.with_name("resolution-macos15-arm64.json")
    resolution.write_bytes(_canonical_json(linux_resolution))
    return constraints, resolution


@pytest.mark.parametrize(
    ("target_id", "architectures"),
    [
        ("wsl2-ubuntu24", {"aarch64", "x86_64"}),
        ("macos15-arm64", {"arm64"}),
    ],
)
def test_schema4_platform_bundle_round_trip(
    tmp_path: Path, target_id: str, architectures: set[str]
) -> None:
    inputs = _write_trial_inputs(tmp_path)
    output = _assemble_platform(inputs, tmp_path / "bundle", target_id)

    verified = _module().verify_trial_bundle_bytes(
        {path.name: path.read_bytes() for path in output.iterdir()}
    )

    assert verified["schema"] == 4
    assert verified["target"]["id"] == target_id
    assert set(verified["architectures"]) == architectures


def test_schema4_rejects_target_architecture_and_filename_mismatch(
    tmp_path: Path,
) -> None:
    inputs = _write_trial_inputs(tmp_path)
    constraints, resolution = _macos_evidence(inputs)

    with pytest.raises(_module().TrialBundleError, match="filename"):
        _module().assemble_platform_trial_bundle(
            output_directory=tmp_path / "bundle",
            wheel=inputs["wheel"],
            preliminary_trial_manifest=inputs["trial_manifest"],
            source_manifest=inputs["source_manifest"],
            staging_manifest=inputs["staging_manifest"],
            architecture_evidence={
                "arm64": (inputs["constraints_aarch64"], resolution)
            },
            quick_start=inputs["quick_start"],
            quick_start_ja=inputs["quick_start_ja"],
            feedback_ja=inputs["feedback"],
            trial_target_id="macos15-arm64",
            repository="example/gwexpy-studio",
            build_workflow_path=".github/workflows/build-trial-wheel.yml",
            build_run_id=123456,
            build_run_number=1,
            build_run_attempt=1,
        )


@pytest.mark.parametrize(
    "mutation", ["target", "architecture_filename", "quick_start_source"]
)
def test_schema4_verifier_rejects_rebound_contract_mismatch(
    tmp_path: Path, mutation: str
) -> None:
    inputs = _write_trial_inputs(tmp_path)
    output = _assemble_platform(inputs, tmp_path / "bundle", "wsl2-ubuntu24")
    manifest_path = output / "TRIAL-MANIFEST.json"
    manifest = json.loads(manifest_path.read_bytes())
    if mutation == "target":
        manifest["target"]["host_os"] = "macos"
    elif mutation == "architecture_filename":
        manifest["architectures"]["x86_64"]["constraints"]["filename"] = (
            "constraints-ubuntu24-aarch64.txt"
        )
    else:
        manifest["quick_start"]["en"]["source_path"] = "docs/trial/macos/Quick-Start.md"
    manifest_path.write_bytes(_canonical_json(manifest))
    _refresh_checksums(output)

    with pytest.raises(_module().TrialBundleError):
        _module().verify_trial_bundle_bytes(
            {path.name: path.read_bytes() for path in output.iterdir()}
        )


@pytest.mark.parametrize("target_id", ["wsl2-ubuntu24", "macos15-arm64"])
def test_schema4_archive_name_and_root_include_target(
    tmp_path: Path, target_id: str
) -> None:
    from scripts.package_trial_release import (
        package_trial_release,
        verify_trial_release,
    )

    inputs = _write_trial_inputs(tmp_path)
    bundle = _assemble_platform(inputs, tmp_path / "bundle", target_id)
    archive, sidecar = package_trial_release(bundle, tmp_path / "release")

    assert archive.name.startswith(f"gwexpy-studio-trial-{target_id}-P-")
    manifest = verify_trial_release(archive, sidecar)
    assert manifest["target"]["id"] == target_id


@pytest.mark.parametrize("target_id", ["wsl2-ubuntu24", "macos15-arm64"])
def test_schema4_archive_is_reproducible(tmp_path: Path, target_id: str) -> None:
    from scripts.package_trial_release import package_trial_release

    inputs = _write_trial_inputs(tmp_path)
    bundle = _assemble_platform(inputs, tmp_path / "bundle", target_id)

    first, first_sidecar = package_trial_release(bundle, tmp_path / "first")
    second, second_sidecar = package_trial_release(bundle, tmp_path / "second")

    assert first.read_bytes() == second.read_bytes()
    assert first_sidecar.read_bytes() == second_sidecar.read_bytes()
