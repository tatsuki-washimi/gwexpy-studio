"""Contracts for the source-bound conda trial toolchain."""

from __future__ import annotations

import hashlib
import importlib
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.unit


def _module():
    return importlib.import_module("scripts.trial_toolchain")


def test_toolchain_manifest_contains_the_pinned_public_bootstrap_inputs() -> None:
    manifest = _module().load_toolchain_manifest()

    assert manifest["schema"] == 1
    assert manifest["miniforge"] == {
        "release": "26.7.2-0",
        "artifacts": {
            "Linux-x86_64": {
                "filename": "Miniforge3-26.7.2-0-Linux-x86_64.sh",
                "sha256": (
                    "281b0ac7d550802efc81af633225a5e6116d29ae72f3ab4eae7168c3931a4c05"
                ),
            },
            "Linux-aarch64": {
                "filename": "Miniforge3-26.7.2-0-Linux-aarch64.sh",
                "sha256": (
                    "89b786c8d2c8b0fda7553914c1314ae4ddaa094503802f279377b19ac4463cb2"
                ),
            },
            "MacOSX-arm64": {
                "filename": "Miniforge3-26.7.2-0-MacOSX-arm64.sh",
                "sha256": (
                    "d70bfa2e97afcda96927c9b9ca0e2316cb7750e4ce651c94388267cbe9588711"
                ),
            },
        },
    }
    assert manifest["debian13_container"] == {
        "image": "debian:13-slim",
        "platform": "linux/amd64",
        "digest": (
            "sha256:abc9cb88a5587630d7f915f47b23b0668fe250fbfc6457aa4d52b534c1bbf73f"
        ),
    }


@pytest.mark.parametrize(
    ("target_id", "architecture", "subdir"),
    [
        ("ubuntu24-x86_64", "x86_64", "linux-64"),
        ("debian13-x86_64", "x86_64", "linux-64"),
        ("wsl2-ubuntu24", "aarch64", "linux-aarch64"),
        ("macos15-arm64", "arm64", "osx-arm64"),
    ],
)
def test_conda_subdir_is_bound_to_the_closed_target(
    target_id: str, architecture: str, subdir: str
) -> None:
    assert _module().conda_subdir(target_id, architecture) == subdir


def test_conda_constructor_is_explicit_and_never_a_venv_fallback() -> None:
    command = _module().fresh_conda_create_command(
        conda_executable=Path("/opt/miniforge/bin/conda"),
        target_id="ubuntu24-x86_64",
        architecture="x86_64",
        prefix=Path("/tmp/trial-conda"),
    )

    assert command == (
        "/opt/miniforge/bin/conda",
        "create",
        "--yes",
        "--quiet",
        "--override-channels",
        "--channel",
        "conda-forge",
        "--prefix",
        "/tmp/trial-conda",
        "python=3.12",
        "pip",
    )
    assert "venv" not in command


def test_conda_reconstructor_passes_the_phase_one_closure_as_exact_matchspecs() -> None:
    command = _module().fresh_conda_create_command(
        conda_executable=Path("/opt/miniforge/bin/conda"),
        target_id="ubuntu24-x86_64",
        architecture="x86_64",
        prefix=Path("/tmp/trial-conda-phase-two"),
        package_records=[
            {"build": "h456_0", "name": "python", "version": "3.12.14"},
            {"build": "20_gnu", "name": "_openmp_mutex", "version": "4.5"},
        ],
    )

    assert command[-2:] == ("_openmp_mutex=4.5=20_gnu", "python=3.12.14=h456_0")
    assert "python=3.12" not in command
    assert "pip" not in command


def test_conda_reconstructor_rejects_an_empty_phase_one_closure() -> None:
    with pytest.raises(_module().ToolchainError, match="empty"):
        _module().fresh_conda_create_command(
            conda_executable=Path("/opt/miniforge/bin/conda"),
            target_id="ubuntu24-x86_64",
            architecture="x86_64",
            prefix=Path("/tmp/trial-conda-phase-two"),
            package_records=[],
        )


def test_conda_manager_record_binds_target_subdir_and_required_specs() -> None:
    record = _module().conda_environment_manager_record(
        target_id="debian13-x86_64",
        architecture="x86_64",
        version="25.7.0",
    )

    assert record == {
        "kind": "conda",
        "requested_specs": ["python=3.12", "pip"],
        "subdir": "linux-64",
        "version": "25.7.0",
    }


def test_conda_manager_record_reads_platform_from_conda_info() -> None:
    record = _module().conda_environment_manager_record_from_info(
        target_id="ubuntu24-x86_64",
        architecture="x86_64",
        info={"conda_version": "26.7.2", "platform": "linux-64"},
    )

    assert record == {
        "kind": "conda",
        "requested_specs": ["python=3.12", "pip"],
        "subdir": "linux-64",
        "version": "26.7.2",
    }


def test_conda_manager_record_rejects_info_without_platform() -> None:
    with pytest.raises(_module().ToolchainError, match="platform"):
        _module().conda_environment_manager_record_from_info(
            target_id="ubuntu24-x86_64",
            architecture="x86_64",
            info={"conda_version": "26.7.2", "subdir": "linux-64"},
        )


def test_conda_phase_packages_projects_conda_list_without_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = importlib.import_module("scripts.capture_trial_resolution")
    conda_json = (
        b'[{"build_string":"20_gnu","name":"_openmp_mutex","version":"4.5",'
        b'"channel":"conda-forge","prefix":"/private/prefix"},'
        b'{"build_string":"h456_0","name":"python","version":"3.12.14",'
        b'"channel":"conda-forge","prefix":"/private/prefix"},'
        b'{"build_string":"h123_0","name":"pip","version":"25.3",'
        b'"channel":"conda-forge","prefix":"/private/prefix"}]'
    )

    def run(command: tuple[str, ...], **_kwargs: object) -> SimpleNamespace:
        assert command == (
            "/opt/miniforge/bin/conda",
            "list",
            "--json",
            "--no-pip",
            "--prefix",
            str(tmp_path / "prefix"),
        )
        return SimpleNamespace(returncode=0, stdout=conda_json, stderr=b"")

    monkeypatch.setattr(module.subprocess, "run", run)

    assert module._conda_phase_packages(
        conda_executable=Path("/opt/miniforge/bin/conda"),
        prefix=tmp_path / "prefix",
        cwd=tmp_path,
    ) == [
        {"build": "20_gnu", "name": "_openmp_mutex", "version": "4.5"},
        {"build": "h123_0", "name": "pip", "version": "25.3"},
        {"build": "h456_0", "name": "python", "version": "3.12.14"},
    ]


def test_sha256_file_reports_the_exact_bootstrap_bytes(tmp_path: Path) -> None:
    artifact = tmp_path / "Miniforge3-test.sh"
    content = b"verified bootstrap bytes\n"
    artifact.write_bytes(content)

    assert _module().sha256_file(artifact) == hashlib.sha256(content).hexdigest()


def test_miniforge_selectors_bind_host_and_debian_image_to_manifest() -> None:
    module = _module()

    assert module.miniforge_artifact(system="linux", architecture="x86_64") == {
        "filename": "Miniforge3-26.7.2-0-Linux-x86_64.sh",
        "sha256": "281b0ac7d550802efc81af633225a5e6116d29ae72f3ab4eae7168c3931a4c05",
    }
    assert module.miniforge_download_url(
        system="darwin", architecture="arm64"
    ).endswith("/26.7.2-0/Miniforge3-26.7.2-0-MacOSX-arm64.sh")
    assert module.debian_container_reference() == (
        "debian:13-slim@sha256:abc9cb88a5587630d7f915f47b23b0668fe250fbfc6457aa4d52b534c1bbf73f"
    )


def test_verified_bootstrap_rejects_bad_bytes_before_subprocess(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = importlib.import_module("scripts.bootstrap_trial_conda")
    installer = tmp_path / "Miniforge.sh"
    installer.write_bytes(b"tampered installer")
    calls: list[object] = []

    monkeypatch.setattr(
        module,
        "miniforge_artifact",
        lambda **_: {"filename": installer.name, "sha256": "0" * 64},
    )
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *args, **kwargs: calls.append(args),
    )

    with pytest.raises(module.BootstrapError, match="SHA-256"):
        module.install_verified_miniforge(
            installer=installer,
            prefix=tmp_path / "miniforge",
            system="linux",
            architecture="x86_64",
        )

    assert calls == []
    assert not (tmp_path / "miniforge").exists()


@pytest.mark.parametrize(
    ("target_id", "architecture"),
    [("macos15-arm64", "x86_64"), ("debian13-x86_64", "aarch64")],
)
def test_conda_constructor_rejects_target_architecture_mismatch(
    target_id: str, architecture: str
) -> None:
    with pytest.raises(_module().ToolchainError, match="architecture"):
        _module().fresh_conda_create_command(
            conda_executable=Path("/opt/miniforge/bin/conda"),
            target_id=target_id,
            architecture=architecture,
            prefix=Path("/tmp/trial-conda"),
        )
