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


def _assemble_platform(
    inputs: dict[str, Path], output: Path, target_id: str, *, prepare: bool = True
) -> Path:
    if target_id == "wsl2-ubuntu24":
        evidence = {
            architecture: (
                inputs[f"constraints_{architecture}"],
                (
                    _wsl2_evidence(inputs[f"resolution_{architecture}"], architecture)
                    if prepare
                    else inputs[f"resolution_{architecture}"]
                ),
            )
            for architecture in ("aarch64", "x86_64")
        }
    else:
        evidence = {"arm64": _macos_evidence(inputs)} if prepare else {
            "arm64": (
                inputs["constraints_aarch64"].with_name("constraints-macos15-arm64.txt"),
                inputs["resolution_aarch64"].with_name("resolution-macos15-arm64.json"),
            )
        }
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


def _wsl2_evidence(resolution_path: Path, architecture: str) -> Path:
    """Convert the retained legacy fixture into explicit schema-4 evidence."""
    resolution = json.loads(resolution_path.read_bytes())
    resolution["environment_manager"] = {
        "kind": "conda",
        "requested_specs": ["python=3.12", "pip"],
        "subdir": "linux-aarch64" if architecture == "aarch64" else "linux-64",
        "version": "26.7.2",
    }
    resolution["schema"] = 4
    resolution["target_id"] = "wsl2-ubuntu24"
    _upgrade_schema4_phase_evidence(resolution, architecture, linux=True)
    resolution_path.write_bytes(_canonical_json(resolution))
    return resolution_path


def _upgrade_schema4_phase_evidence(
    resolution: dict[str, object], architecture: str, *, linux: bool
) -> None:
    """Turn a legacy fixture into bounded current schema-4 evidence."""
    gate = resolution["technical_gate"]
    assert isinstance(gate, dict)
    gate["schema"] = 3
    gate["checks"] = {name: True for name in _gate_check_names()}
    conda_packages = [
        {"build": "20_gnu", "name": "_openmp_mutex", "version": "4.5"},
        {
            "build": "h123_0",
            "name": "pip",
            "version": resolution["pip_version"],
        },
        {
            "build": "h456_0",
            "name": "python",
            "version": resolution["python_version"],
        },
    ]
    for phase_name in ("phase_one", "phase_two"):
        phase = resolution[phase_name]
        assert isinstance(phase, dict)
        phase["conda_packages"] = conda_packages
        phase["import_isolation"] = _import_isolation(studio=True)
    first = resolution["phase_one"]
    assert isinstance(first, dict)
    replay_artifacts = [
        item for item in first["artifacts"] if item["name"] != "gwexpy-studio"
    ]
    replay_inspect = {
        "installed": [
            item
            for item in first["inspect"]["installed"]
            if item["name"] != "gwexpy-studio"
        ]
    }
    replay = {
        "artifacts": replay_artifacts,
        "conda_packages": conda_packages,
        "import_isolation": _import_isolation(studio=False),
        "inspect": replay_inspect,
        "report": {"artifacts": replay_artifacts},
    }
    replay["pip_inspect_sha256"] = _sha256(_canonical_json(replay["inspect"]))
    replay["pip_report_sha256"] = _sha256(_canonical_json(replay["report"]))
    resolution["phase_replay"] = replay
    if linux:
        runtime = resolution["os_runtime"]
        assert isinstance(runtime, dict)
        packages = runtime["packages"]
        assert isinstance(packages, list)
        dpkg_architecture = "amd64" if architecture == "x86_64" else "arm64"
        existing = {item["name"] for item in packages}
        packages.extend(
            {
                "architecture": dpkg_architecture,
                "name": name,
                "status": "installed",
                "version": "1.0.0-1build1",
            }
            for name in (
                "libfontconfig1",
                "libglib2.0-0t64",
                "libdbus-1-3",
                "libxkbcommon0",
                "libzstd1",
            )
            if name not in existing
        )


def _gate_check_names() -> tuple[str, ...]:
    from scripts.run_trial_technical_gate import _CHECK_NAMES

    return _CHECK_NAMES


def _import_isolation(*, studio: bool) -> dict[str, object]:
    imports = ["gwexpy", "numpy", "PySide6"]
    if studio:
        imports.insert(0, "gwexpy_studio")
    return {
        "checkout_on_sys_path": False,
        "imports": imports,
        "no_user_site": True,
        "python": "3.12",
        "pythonpath": False,
        "studio_visible": studio,
    }


def _macos_evidence(inputs: dict[str, Path]) -> tuple[Path, Path]:
    constraints = inputs["constraints_aarch64"].with_name(
        "constraints-macos15-arm64.txt"
    )
    constraints.write_bytes(inputs["constraints_aarch64"].read_bytes())
    linux_resolution = json.loads(inputs["resolution_aarch64"].read_bytes())
    linux_resolution["architecture"] = "arm64"
    linux_resolution["constraints_sha256"] = _sha256(constraints.read_bytes())
    linux_resolution["schema"] = 4
    linux_resolution["target_id"] = "macos15-arm64"
    linux_resolution["environment_manager"] = {
        "kind": "conda",
        "requested_specs": ["python=3.12", "pip"],
        "subdir": "osx-arm64",
        "version": "26.7.2",
    }
    conda_packages = [
        {"build": "20_gnu", "name": "_openmp_mutex", "version": "4.5"},
        {
            "build": "h123_0",
            "name": "pip",
            "version": linux_resolution["pip_version"],
        },
        {
            "build": "h456_0",
            "name": "python",
            "version": linux_resolution["python_version"],
        },
    ]
    for phase_name in ("phase_one", "phase_two"):
        linux_resolution[phase_name]["conda_packages"] = conda_packages
    _upgrade_schema4_phase_evidence(linux_resolution, "arm64", linux=False)
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


def test_schema4_platform_bundle_accepts_new_conda_resolution_evidence(
    tmp_path: Path,
) -> None:
    inputs = _write_trial_inputs(tmp_path)
    for architecture in ("aarch64", "x86_64"):
        resolution_path = inputs[f"resolution_{architecture}"]
        resolution = json.loads(resolution_path.read_bytes())
        resolution["environment_manager"] = {
            "kind": "conda",
            "requested_specs": ["python=3.12", "pip"],
            "subdir": "linux-aarch64" if architecture == "aarch64" else "linux-64",
            "version": "26.7.2",
        }
        resolution["schema"] = 4
        resolution["target_id"] = "wsl2-ubuntu24"
        _upgrade_schema4_phase_evidence(
            resolution, architecture, linux=True
        )
        resolution_path.write_bytes(_canonical_json(resolution))

    output = _assemble_platform(inputs, tmp_path / "bundle", "wsl2-ubuntu24")

    verified = _module().verify_trial_bundle_bytes(
        {path.name: path.read_bytes() for path in output.iterdir()}
    )
    assert verified["schema"] == 4


def test_schema4_platform_bundle_rejects_a_legacy_gate_record(
    tmp_path: Path,
) -> None:
    inputs = _write_trial_inputs(tmp_path)
    for architecture in ("aarch64", "x86_64"):
        resolution_path = _wsl2_evidence(
            inputs[f"resolution_{architecture}"], architecture
        )
        resolution = json.loads(resolution_path.read_bytes())
        gate = resolution["technical_gate"]
        assert isinstance(gate, dict)
        gate["schema"] = 2
        gate["checks"] = {
            name: gate["checks"][name]
            for name in (
                "launcher_import",
                "welcome",
                "try_sample",
                "crop",
                "asd",
                "save_project",
                "project_reopen",
                "recovery",
                "worker_exit",
                "shared_memory_cleanup",
            )
        }
        resolution_path.write_bytes(_canonical_json(resolution))

    with pytest.raises(
        _module().TrialBundleError, match="current technical-gate checks"
    ):
        _assemble_platform(
            inputs, tmp_path / "bundle", "wsl2-ubuntu24", prepare=False
        )


def test_explicit_target_assembly_rejects_legacy_resolution_schema(
    tmp_path: Path,
) -> None:
    """New target bundles cannot relabel historical schema-2 evidence."""
    inputs = _write_trial_inputs(tmp_path)
    evidence = {
        architecture: (
            inputs[f"constraints_{architecture}"],
            inputs[f"resolution_{architecture}"],
        )
        for architecture in ("aarch64", "x86_64")
    }

    with pytest.raises(_module().TrialBundleError, match="schema 4"):
        _module().assemble_platform_trial_bundle(
            output_directory=tmp_path / "bundle",
            wheel=inputs["wheel"],
            preliminary_trial_manifest=inputs["trial_manifest"],
            source_manifest=inputs["source_manifest"],
            staging_manifest=inputs["staging_manifest"],
            architecture_evidence=evidence,
            quick_start=inputs["quick_start"],
            quick_start_ja=inputs["quick_start_ja"],
            feedback_ja=inputs["feedback"],
            trial_target_id="wsl2-ubuntu24",
            repository="example/gwexpy-studio",
            build_workflow_path=".github/workflows/build-trial-wheel.yml",
            build_run_id=123456,
            build_run_number=1,
            build_run_attempt=1,
        )


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
