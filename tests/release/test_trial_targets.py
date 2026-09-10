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


def test_unknown_target_and_wrong_architecture_fail_closed() -> None:
    module = _module()

    with pytest.raises(module.TrialTargetError, match="unsupported trial target"):
        module.trial_target("ubuntu")
    with pytest.raises(module.TrialTargetError, match="architecture"):
        module.trial_target("macos15-arm64").constraints_filename("x86_64")
