"""Cross-platform trial target contracts."""

from __future__ import annotations

import importlib

import pytest

pytestmark = pytest.mark.unit


def _module():
    return importlib.import_module("scripts.trial_targets")


def test_wsl2_target_has_two_native_ubuntu_architectures() -> None:
    target = _module().trial_target("wsl2-ubuntu24")

    assert target.architectures == ("x86_64", "aarch64")
    assert target.constraints_filename("x86_64") == ("constraints-ubuntu24-x86_64.txt")
    assert target.resolution_filename("aarch64") == ("resolution-ubuntu24-aarch64.json")
    assert target.manifest_record() == {
        "architectures": ["x86_64", "aarch64"],
        "guest_os": {"id": "ubuntu", "version": "24.04"},
        "host_min_version": "11",
        "host_os": "windows",
        "id": "wsl2-ubuntu24",
    }
    assert target.contract_record()["backend"] == "wayland"
    assert target.contract_record()["allowed_backends"] == ["wayland", "xcb"]
    assert target.contract_record()["hosted_backend"] == "offscreen"


def test_macos_target_is_native_arm64_only() -> None:
    target = _module().trial_target("macos15-arm64")

    assert target.architectures == ("arm64",)
    assert target.constraints_filename("arm64") == ("constraints-macos15-arm64.txt")
    assert target.resolution_filename("arm64") == "resolution-macos15-arm64.json"
    assert target.manifest_record() == {
        "architectures": ["arm64"],
        "guest_os": None,
        "host_min_version": "15",
        "host_os": "macos",
        "id": "macos15-arm64",
    }
    assert target.allowed_backends == ("cocoa",)
    assert target.hosted_backend == "cocoa"


@pytest.mark.parametrize(
    (
        "target_id",
        "host_os",
        "version",
        "backend",
        "constraints",
        "resolution",
        "docs",
        "group",
    ),
    [
        (
            "ubuntu24-x86_64",
            "ubuntu",
            "24.04",
            "wayland",
            "constraints-ubuntu24-x86_64.txt",
            "resolution-ubuntu24-x86_64.json",
            "docs/trial/ubuntu",
            "ubuntu",
        ),
        (
            "debian13-x86_64",
            "debian",
            "13",
            "wayland",
            "constraints-debian13-x86_64.txt",
            "resolution-debian13-x86_64.json",
            "docs/trial/debian",
            "debian",
        ),
    ],
)
def test_native_targets_have_explicit_distribution_contract(
    target_id: str,
    host_os: str,
    version: str,
    backend: str,
    constraints: str,
    resolution: str,
    docs: str,
    group: str,
) -> None:
    target = _module().trial_target(target_id)

    assert target.host_os == host_os
    assert target.host_min_version == version
    assert target.guest_os is None
    assert target.architectures == ("x86_64",)
    assert target.mode == "native"
    assert target.backend == backend
    assert target.constraints_filename("x86_64") == constraints
    assert target.resolution_filename("x86_64") == resolution
    assert target.documentation_directory == docs
    assert target.publication_group == group
    assert target.allowed_backends == ("wayland", "xcb")
    assert target.hosted_backend == "offscreen"


def test_target_registry_is_closed_and_keeps_canonical_sort_order() -> None:
    assert _module().target_ids() == (
        "debian13-x86_64",
        "macos15-arm64",
        "ubuntu24-x86_64",
        "wsl2-ubuntu24",
    )


def test_wsl_target_declares_guest_mode_backend_and_publication_contract() -> None:
    target = _module().trial_target("wsl2-ubuntu24")

    assert target.mode == "wsl2"
    assert target.backend == "wayland"
    assert target.allowed_backends == ("wayland", "xcb")
    target.require_backend("xcb")
    with pytest.raises(_module().TrialTargetError, match="backend"):
        target.require_backend("offscreen")
    assert target.publication_group == "wsl2-mac"
    assert target.documentation_directory == "docs/trial/wsl2"
    assert target.constraints_filenames == (
        ("x86_64", "constraints-ubuntu24-x86_64.txt"),
        ("aarch64", "constraints-ubuntu24-aarch64.txt"),
    )


def test_unknown_target_and_wrong_architecture_fail_closed() -> None:
    module = _module()

    with pytest.raises(module.TrialTargetError, match="unsupported trial target"):
        module.trial_target("ubuntu")
    with pytest.raises(module.TrialTargetError, match="architecture"):
        module.trial_target("macos15-arm64").constraints_filename("x86_64")
    with pytest.raises(module.TrialTargetError, match="backend"):
        module.trial_target("macos15-arm64").require_backend("wayland")
    with pytest.raises(module.TrialTargetError, match="unsupported trial target"):
        module.trial_target("linux")
