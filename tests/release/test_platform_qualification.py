"""Physical platform qualification evidence contracts."""

from __future__ import annotations

import hashlib
import importlib
import json

import pytest

pytestmark = pytest.mark.unit

BUILD_ID = "P-abcdef0-20260910-r8-a1"
SOURCE_SHA = "abcdef0" + "1" * 33
WHEEL_SHA = "2" * 64


def _module():
    return importlib.import_module("scripts.verify_platform_qualification")


def _result(
    target_id: str,
    architecture: str,
    *,
    build_id: str = BUILD_ID,
    source_sha: str = SOURCE_SHA,
    wheel_sha: str = WHEEL_SHA,
) -> bytes:
    module = _module()
    checks = {name: True for name in module.required_checks(target_id)}
    if target_id == "wsl2-ubuntu24":
        host = {
            "guest_architecture": architecture,
            "guest_os": "ubuntu",
            "guest_version": "24.04",
            "kernel_release": "6.6.87.2-microsoft-standard-WSL2",
            "python_version": "3.12.12",
            "windows_architecture": architecture,
            "windows_version": "11.0.26100",
        }
    else:
        host = {
            "architecture": "arm64",
            "macos_version": "15.7.1",
            "python_version": "3.12.12",
        }
    return module.qualification_result_bytes(
        target_id=target_id,
        architecture=architecture,
        build_id=build_id,
        source_sha=source_sha,
        wheel_sha256=wheel_sha,
        host=host,
        checks=checks,
    )


def test_wsl2_summary_requires_both_architectures_and_binds_identity() -> None:
    module = _module()
    results = [_result("wsl2-ubuntu24", "x86_64"), _result("wsl2-ubuntu24", "aarch64")]

    summary = module.qualification_summary_bytes(results)
    document = json.loads(summary)

    assert document == {
        "build_id": BUILD_ID,
        "results": [
            {
                "architecture": "aarch64",
                "sha256": hashlib.sha256(results[1]).hexdigest(),
            },
            {
                "architecture": "x86_64",
                "sha256": hashlib.sha256(results[0]).hexdigest(),
            },
        ],
        "schema": 1,
        "source_sha": SOURCE_SHA,
        "status": "passed",
        "target_id": "wsl2-ubuntu24",
        "wheel_sha256": WHEEL_SHA,
    }
    assert summary.endswith(b"\n")


def test_macos_summary_accepts_one_arm64_result() -> None:
    module = _module()

    summary = json.loads(
        module.qualification_summary_bytes([_result("macos15-arm64", "arm64")])
    )

    assert summary["target_id"] == "macos15-arm64"
    assert summary["results"][0]["architecture"] == "arm64"
    assert summary["status"] == "passed"


def test_campaign_summary_requires_all_three_physical_configurations() -> None:
    module = _module()
    mac_build_id = "P-abcdef0-20260910-r9-a1"
    mac_wheel_sha = "3" * 64
    results = [
        _result("wsl2-ubuntu24", "x86_64"),
        _result("wsl2-ubuntu24", "aarch64"),
        _result(
            "macos15-arm64",
            "arm64",
            build_id=mac_build_id,
            wheel_sha=mac_wheel_sha,
        ),
    ]

    summary = module.qualification_campaign_summary_bytes(results)
    document = json.loads(summary)

    assert document["source_sha"] == SOURCE_SHA
    assert document["status"] == "passed"
    assert document["schema"] == 1
    assert [target["target_id"] for target in document["targets"]] == [
        "macos15-arm64",
        "wsl2-ubuntu24",
    ]
    assert document["targets"][0]["build_id"] == mac_build_id
    assert document["targets"][0]["wheel_sha256"] == mac_wheel_sha
    assert len(document["targets"][1]["results"]) == 2


@pytest.mark.parametrize("mutation", ["missing_target", "source_mismatch"])
def test_campaign_summary_rejects_incomplete_or_mixed_source_evidence(
    mutation: str,
) -> None:
    module = _module()
    results = [
        _result("wsl2-ubuntu24", "x86_64"),
        _result("wsl2-ubuntu24", "aarch64"),
        _result("macos15-arm64", "arm64", build_id="P-abcdef0-20260910-r9-a1"),
    ]
    if mutation == "missing_target":
        results.pop()
    else:
        results[-1] = _result(
            "macos15-arm64",
            "arm64",
            build_id="P-4444444-20260910-r9-a1",
            source_sha="4" * 40,
        )

    with pytest.raises(module.QualificationError):
        module.qualification_campaign_summary_bytes(results)


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "failed", "identity"])
def test_summary_rejects_incomplete_or_untrusted_results(mutation: str) -> None:
    module = _module()
    first = _result("wsl2-ubuntu24", "x86_64")
    second = _result("wsl2-ubuntu24", "aarch64")
    values = [first, second]
    if mutation == "missing":
        values = values[:1]
    elif mutation == "duplicate":
        values = [first, first]
    else:
        document = json.loads(second)
        if mutation == "failed":
            check = next(iter(document["checks"]))
            document["checks"][check] = False
            document["status"] = "failed"
        else:
            document["source_sha"] = "3" * 40
        second = (
            json.dumps(document, sort_keys=True, separators=(",", ":")).encode() + b"\n"
        )
        values = [first, second]

    with pytest.raises(module.QualificationError):
        module.qualification_summary_bytes(values)


def test_result_rejects_paths_and_unknown_check_names() -> None:
    module = _module()
    checks = {name: True for name in module.required_checks("macos15-arm64")}
    checks["unexpected"] = True

    with pytest.raises(module.QualificationError):
        module.qualification_result_bytes(
            target_id="macos15-arm64",
            architecture="arm64",
            build_id=BUILD_ID,
            source_sha=SOURCE_SHA,
            wheel_sha256=WHEEL_SHA,
            host={
                "architecture": "arm64",
                "macos_version": "/Users/alice/private",
                "python_version": "3.12.12",
            },
            checks=checks,
        )
