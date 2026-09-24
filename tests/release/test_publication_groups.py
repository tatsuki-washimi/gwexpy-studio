"""Schema-2 publication group and audit proof contracts."""

from __future__ import annotations

import copy
import hashlib
import json
import zipfile
from pathlib import Path

import pytest

from scripts.release_source_manifest import (
    ManifestEntry,
    ReleaseSourceManifest,
    read_manifest,
)
from tests.release.test_platform_trial_bundle import (
    _assemble_platform,
    _upgrade_schema4_phase_evidence,
)
from tests.release.test_trial_bundle import _write_trial_inputs, _write_wheel_members

pytestmark = pytest.mark.unit


def _canonical(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _result(
    target_id: str,
    architecture: str,
    *,
    build_id: str,
    source_sha: str,
    wheel_sha256: str,
    zip_sha256: str,
    kit_manifest_sha256: str,
) -> bytes:
    from scripts.verify_platform_qualification import (
        qualification2_result_bytes,
        required_schema2_automated_checks,
        required_schema2_owner_confirmations,
    )

    if target_id.startswith("wsl2"):
        host = {
            "guest_architecture": architecture,
            "guest_os": "ubuntu",
            "guest_version": "24.04",
            "kernel_release": "5.15.153-microsoft-standard-WSL2",
            "python_version": "3.12.14",
            "qt_backend": "wayland",
            "windows_architecture": architecture,
            "windows_version": "11.0",
        }
    elif target_id in {"ubuntu24-x86_64", "debian13-x86_64"}:
        host = {
            "architecture": architecture,
            "host_os": "ubuntu" if target_id.startswith("ubuntu") else "debian",
            "os_version": "24.04" if target_id.startswith("ubuntu") else "13",
            "python_version": "3.12.14",
            "qt_backend": "wayland",
        }
    else:
        host = {
            "architecture": architecture,
            "macos_version": "15.7.1",
            "python_version": "3.12.14",
            "qt_backend": "cocoa",
        }
    return qualification2_result_bytes(
        target_id=target_id,
        architecture=architecture,
        build_id=build_id,
        source_sha=source_sha,
        wheel_sha256=wheel_sha256,
        zip_sha256=zip_sha256,
        kit_manifest_sha256=kit_manifest_sha256,
        host=host,
        automated_checks={
            name: True for name in required_schema2_automated_checks(target_id)
        },
        owner_confirmations={
            name: True for name in required_schema2_owner_confirmations(target_id)
        },
        cleanup=True,
        stage="complete",
    )


def _wrapper(path: Path, files: Path) -> Path:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
        for member in sorted(files.iterdir(), key=lambda item: item.name.encode()):
            archive.writestr(member.name, member.read_bytes())
    return path


def _rewrite_release_member(
    release_directory: Path, suffix: str, replacement: bytes
) -> None:
    """Rewrite one real release member and its outer checksum sidecar."""
    archive_path = next(release_directory.glob("*.zip"))
    with zipfile.ZipFile(archive_path) as archive:
        members = [(info, archive.read(info)) for info in archive.infolist()]
    changed = False
    rewritten: list[tuple[zipfile.ZipInfo, bytes]] = []
    for info, content in members:
        if info.filename.endswith(f"/{suffix}"):
            content = replacement
            changed = True
        rewritten.append((info, content))
    assert changed
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_STORED) as archive:
        for info, content in rewritten:
            archive.writestr(info, content)
    digest = _sha(archive_path.read_bytes())
    archive_path.with_name(f"{archive_path.name}.sha256").write_text(
        f"{digest}  {archive_path.name}\n", encoding="ascii"
    )


def _build_record(
    target_id: str,
    release: Path,
    kit: Path,
    *,
    run_id: int = 123456,
    run_number: int = 1,
    run_attempt: int = 1,
    source_sha: str = "abcdef0123456789abcdef0123456789abcdef01",
) -> dict[str, object]:
    release_name = f"trial-bundle-{target_id}-{run_id}-a{run_attempt}"
    kit_name = f"trial-qualification-kit-{target_id}-{run_id}-a{run_attempt}"
    release_wrapper = release.parent / f"{target_id}-release-wrapper.zip"
    kit_wrapper = kit.parent / f"{target_id}-kit-wrapper.zip"
    _wrapper(release_wrapper, release)
    _wrapper(kit_wrapper, kit)
    return {
        "run": {
            "id": run_id,
            "path": ".github/workflows/build-trial-wheel.yml",
            "event": "workflow_dispatch",
            "status": "completed",
            "conclusion": "success",
            "head_branch": "main",
            "head_sha": source_sha,
            "run_number": run_number,
            "run_attempt": run_attempt,
            "repository": {"full_name": "example/gwexpy-studio"},
        },
        "artifacts": [
            {
                "id": 1001 + run_id,
                "name": release_name,
                "digest": f"sha256:{_sha(release_wrapper.read_bytes())}",
                "expired": False,
            },
            {
                "id": 2001 + run_id,
                "name": kit_name,
                "digest": f"sha256:{_sha(kit_wrapper.read_bytes())}",
                "expired": False,
            },
        ],
        "release_wrapper": release_wrapper,
        "kit_wrapper": kit_wrapper,
    }


def _cli_descriptor(
    tmp_path: Path,
    builds: dict[str, object],
    results: dict[str, dict[str, bytes]],
) -> Path:
    """Write the standalone CLI's path-only descriptor from real fixtures."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    result_paths: dict[str, dict[str, str]] = {}
    for target_id, target_results in results.items():
        result_paths[target_id] = {}
        for architecture, raw in target_results.items():
            result_path = tmp_path / f"{target_id}-{architecture}.json"
            result_path.write_bytes(raw)
            result_paths[target_id][architecture] = str(result_path)
    descriptor_builds: dict[str, dict[str, object]] = {}
    for target_id, value in builds.items():
        assert isinstance(value, dict)
        record = dict(value)
        record["release_wrapper"] = str(record["release_wrapper"])
        record["kit_wrapper"] = str(record["kit_wrapper"])
        descriptor_builds[target_id] = record
    descriptor = {
        "builds": descriptor_builds,
        "default_branch": "main",
        "qualification_results": result_paths,
        "repository": "example/gwexpy-studio",
    }
    descriptor_path = tmp_path / "descriptor.json"
    descriptor_path.write_bytes(_canonical(descriptor))
    return descriptor_path


def _extend_platform_source(inputs: dict[str, Path]) -> None:
    """Add native Linux docs and refresh fixture source-bound identities."""
    source = read_manifest(inputs["source_manifest"])
    repository_root = Path(__file__).resolve().parents[2]
    additions = []
    for target_id in ("ubuntu", "debian"):
        for name in ("Quick-Start.md", "Quick-Start.ja.md", "Feedback.ja.md"):
            path = f"docs/trial/{target_id}/{name}"
            content = (repository_root / path).read_bytes()
            additions.append(
                ManifestEntry(
                    path=path,
                    file_type="file",
                    mode="0644",
                    sha256=_sha(content),
                )
            )
    source = ReleaseSourceManifest(
        entries=tuple(
            sorted(
                (*source.entries, *additions),
                key=lambda item: item.path.encode(),
            )
        )
    )
    inputs["source_manifest"].write_bytes(source.to_bytes())
    source_sha256 = _sha(source.to_bytes())

    wheel = inputs["wheel"]
    with zipfile.ZipFile(wheel) as archive:
        members = {name: archive.read(name) for name in archive.namelist()}
    trial_build = json.loads(
        members["gwexpy_studio/assets/trial-build.json"]
    )
    trial_build["source_manifest_sha256"] = source_sha256
    members["gwexpy_studio/assets/trial-build.json"] = _canonical(trial_build)
    _write_wheel_members(wheel, members)
    generated = {
        f"src/{path}": members[path]
        for path in (
            "gwexpy_studio/_version.py",
            "gwexpy_studio/assets/trial-build.json",
            "gwexpy_studio/assets/io-capabilities.json",
        )
    }
    staging_entries = {entry.path: entry for entry in source.entries}
    staging_entries.update(
        {
            path: ManifestEntry(
                path=path,
                file_type="file",
                mode="0644",
                sha256=_sha(content),
            )
            for path, content in generated.items()
        }
    )
    staging = ReleaseSourceManifest(
        entries=tuple(
            sorted(staging_entries.values(), key=lambda item: item.path.encode())
        )
    )
    inputs["staging_manifest"].write_bytes(staging.to_bytes())
    staging_sha256 = _sha(staging.to_bytes())
    for architecture in ("x86_64", "aarch64"):
        resolution_path = inputs[f"resolution_{architecture}"]
        resolution = json.loads(resolution_path.read_bytes())
        resolution["source_manifest_sha256"] = source_sha256
        resolution["staging_manifest_sha256"] = staging_sha256
        wheel_sha256 = _sha(inputs["wheel"].read_bytes())
        resolution["wheel"]["sha256"] = wheel_sha256
        for phase_name in ("phase_one", "phase_two"):
            phase = resolution[phase_name]
            for artifact in phase["artifacts"]:
                if artifact["name"] == "gwexpy-studio":
                    artifact["sha256"] = wheel_sha256
            phase["report"] = {"artifacts": phase["artifacts"]}
            phase["pip_report_sha256"] = _sha(_canonical(phase["report"]))
        resolution_path.write_bytes(_canonical(resolution))
    preliminary = json.loads(inputs["trial_manifest"].read_bytes())
    preliminary["source_manifest_sha256"] = source_sha256
    preliminary["staging_manifest_sha256"] = staging_sha256
    preliminary["wheel_sha256"] = _sha(wheel.read_bytes())
    preliminary["generated_files"] = [
        {"path": path, "sha256": _sha(content)}
        for path, content in generated.items()
    ]
    inputs["trial_manifest"].write_bytes(_canonical(preliminary))


def _assemble_native_linux(
    inputs: dict[str, Path], output: Path, target_id: str
) -> Path:
    """Assemble a fixture native target using schema-4 Linux evidence."""
    target = {"ubuntu24-x86_64": "ubuntu", "debian13-x86_64": "debian"}[target_id]
    filename_target = {
        "ubuntu24-x86_64": "ubuntu24",
        "debian13-x86_64": "debian13",
    }[target_id]
    constraints = output.parent / f"constraints-{filename_target}-x86_64.txt"
    constraints.write_bytes(inputs["constraints_x86_64"].read_bytes())
    resolution = json.loads(inputs["resolution_x86_64"].read_bytes())
    resolution["environment_manager"] = {
        "kind": "conda",
        "requested_specs": ["python=3.12", "pip"],
        "subdir": "linux-64",
        "version": "26.7.2",
    }
    resolution["schema"] = 4
    resolution["target_id"] = target_id
    resolution["constraints_sha256"] = _sha(constraints.read_bytes())
    _upgrade_schema4_phase_evidence(resolution, "x86_64", linux=True)
    if target == "debian":
        resolution["os_id"] = "debian"
        resolution["os_version"] = "13"
    resolution_path = output.parent / f"resolution-{filename_target}-x86_64.json"
    resolution_path.write_bytes(_canonical(resolution))
    docs = Path(__file__).resolve().parents[2] / "docs" / "trial" / target
    from scripts.assemble_trial_bundle import assemble_platform_trial_bundle

    return assemble_platform_trial_bundle(
        output_directory=output,
        wheel=inputs["wheel"],
        preliminary_trial_manifest=inputs["trial_manifest"],
        source_manifest=inputs["source_manifest"],
        staging_manifest=inputs["staging_manifest"],
        architecture_evidence={"x86_64": (constraints, resolution_path)},
        quick_start=docs / "Quick-Start.md",
        quick_start_ja=docs / "Quick-Start.ja.md",
        feedback_ja=docs / "Feedback.ja.md",
        trial_target_id=target_id,
        repository="example/gwexpy-studio",
        build_workflow_path=".github/workflows/build-trial-wheel.yml",
        build_run_id=123456,
        build_run_number=1,
        build_run_attempt=1,
    )


def _wsl_mac_fixture(
    tmp_path: Path,
) -> tuple[dict[str, object], dict[str, dict[str, bytes]]]:
    inputs = _write_trial_inputs(tmp_path)
    _extend_platform_source(inputs)
    records: dict[str, object] = {}
    results: dict[str, dict[str, bytes]] = {}
    for target_id in ("wsl2-ubuntu24", "macos15-arm64"):
        bundle = _assemble_platform(inputs, tmp_path / f"{target_id}-bundle", target_id)
        from scripts.build_qualification_kit import build_qualification_kit
        from scripts.package_trial_release import (
            package_trial_release,
            read_verified_trial_release,
        )

        release = tmp_path / f"{target_id}-release"
        package_trial_release(bundle, release)
        kit = build_qualification_kit(
            release_directory=release,
            output_directory=tmp_path / f"{target_id}-kit",
            target_id=target_id,
        )
        manifest, archive_bytes = read_verified_trial_release(
            next(release.glob("*.zip")), next(release.glob("*.zip.sha256"))
        )
        kit_manifest = (kit / "QUALIFICATION-KIT.json").read_bytes()
        source_sha = manifest["build"]["source_sha"]
        build_id = manifest["build"]["id"]
        wheel_sha = manifest["wheel"]["sha256"]
        zip_sha = _sha(archive_bytes)
        kit_sha = _sha(kit_manifest)
        records[target_id] = _build_record(target_id, release, kit)
        results[target_id] = {
            architecture: _result(
                target_id,
                architecture,
                build_id=build_id,
                source_sha=source_sha,
                wheel_sha256=wheel_sha,
                zip_sha256=zip_sha,
                kit_manifest_sha256=kit_sha,
            )
            for architecture in ("x86_64", "aarch64")
        } if target_id.startswith("wsl2") else {
            "arm64": _result(
                target_id,
                "arm64",
                build_id=build_id,
                source_sha=source_sha,
                wheel_sha256=wheel_sha,
                zip_sha256=zip_sha,
                kit_manifest_sha256=kit_sha,
            )
        }
    return records, results


def _all_fixture(
    tmp_path: Path,
) -> tuple[dict[str, object], dict[str, dict[str, bytes]]]:
    """Create real release/kit wrappers and five schema-2 result bytes."""
    wslmac = tmp_path / "wslmac"
    wslmac.mkdir()
    builds, results = _wsl_mac_fixture(wslmac)
    native = tmp_path / "native"
    native.mkdir()
    inputs = _write_trial_inputs(native)
    _extend_platform_source(inputs)
    from scripts.build_qualification_kit import build_qualification_kit
    from scripts.package_trial_release import (
        package_trial_release,
        read_verified_trial_release,
    )

    for target_id in ("ubuntu24-x86_64", "debian13-x86_64"):
        bundle = _assemble_native_linux(
            inputs, tmp_path / f"{target_id}-bundle", target_id
        )
        release = tmp_path / f"{target_id}-release"
        package_trial_release(bundle, release)
        kit = build_qualification_kit(
            release_directory=release,
            output_directory=tmp_path / f"{target_id}-kit",
            target_id=target_id,
        )
        manifest, archive_bytes = read_verified_trial_release(
            next(release.glob("*.zip")), next(release.glob("*.zip.sha256"))
        )
        source_sha = manifest["build"]["source_sha"]
        build_id = manifest["build"]["id"]
        wheel_sha = manifest["wheel"]["sha256"]
        kit_sha = _sha((kit / "QUALIFICATION-KIT.json").read_bytes())
        records = _build_record(target_id, release, kit)
        builds[target_id] = records
        results[target_id] = {
            "x86_64": _result(
                target_id,
                "x86_64",
                build_id=build_id,
                source_sha=source_sha,
                wheel_sha256=wheel_sha,
                zip_sha256=_sha(archive_bytes),
                kit_manifest_sha256=kit_sha,
            )
        }
    return builds, results


def test_wsl_mac_group_summary_uses_verified_real_bytes(tmp_path: Path) -> None:
    from scripts.verify_publication_groups import publication_group_summary_bytes

    builds, results = _wsl_mac_fixture(tmp_path)
    summary = json.loads(
        publication_group_summary_bytes(
            group_id="wsl2-mac",
            builds=builds,
            qualification_results=results,
            repository="example/gwexpy-studio",
            default_branch="main",
        )
    )

    assert summary["schema"] == 2
    assert summary["kind"] == "publication-group"
    assert summary["publication_group"] == "wsl2-mac"
    assert [row["target_id"] for row in summary["targets"]] == [
        "macos15-arm64",
        "wsl2-ubuntu24",
    ]
    wsl = next(row for row in summary["targets"] if row["target_id"].startswith("wsl2"))
    assert {row["architecture"] for row in wsl["qualification_results"]} == {
        "x86_64",
        "aarch64",
    }
    assert summary["source_sha"] == "abcdef0123456789abcdef0123456789abcdef01"
    assert summary == json.loads(
        publication_group_summary_bytes(
            group_id="wsl2-mac",
            builds=builds,
            qualification_results=results,
            repository="example/gwexpy-studio",
            default_branch="main",
        )
    )


def test_group_rejects_tampered_downloaded_artifact_wrapper(tmp_path: Path) -> None:
    from scripts.verify_publication_groups import (
        PublicationGroupError,
        publication_group_summary_bytes,
    )

    builds, results = _wsl_mac_fixture(tmp_path)
    wsl = builds["wsl2-ubuntu24"]
    assert isinstance(wsl, dict)
    wrapper = wsl["release_wrapper"]
    assert isinstance(wrapper, Path)
    wrapper.write_bytes(wrapper.read_bytes() + b"tampered")

    with pytest.raises(PublicationGroupError, match="digest"):
        publication_group_summary_bytes(
            group_id="wsl2-mac",
            builds=builds,
            qualification_results=results,
            repository="example/gwexpy-studio",
            default_branch="main",
        )


def test_group_rejects_self_reported_result_identity_mismatch(tmp_path: Path) -> None:
    from scripts.verify_publication_groups import (
        PublicationGroupError,
        publication_group_summary_bytes,
    )

    builds, results = _wsl_mac_fixture(tmp_path)
    tampered = json.loads(results["macos15-arm64"]["arm64"])
    tampered["wheel_sha256"] = "b" * 64
    results["macos15-arm64"]["arm64"] = _canonical(tampered)
    with pytest.raises(PublicationGroupError, match="wheel|identity"):
        publication_group_summary_bytes(
            group_id="wsl2-mac",
            builds=builds,
            qualification_results=results,
            repository="example/gwexpy-studio",
            default_branch="main",
        )


@pytest.mark.parametrize(
    "field,value",
    (
        ("build_id", "P-bbbbbbb-20000101-r1-a1"),
        ("source_sha", "b" * 40),
        ("zip_sha256", "b" * 64),
        ("kit_manifest_sha256", "b" * 64),
    ),
)
def test_group_rejects_all_result_identity_hashes(
    tmp_path: Path, field: str, value: str
) -> None:
    from scripts.verify_publication_groups import (
        PublicationGroupError,
        publication_group_summary_bytes,
    )

    builds, results = _wsl_mac_fixture(tmp_path)
    tampered = json.loads(results["macos15-arm64"]["arm64"])
    tampered[field] = value
    results["macos15-arm64"]["arm64"] = _canonical(tampered)
    with pytest.raises(PublicationGroupError, match="identity or status"):
        publication_group_summary_bytes(
            group_id="wsl2-mac",
            builds=builds,
            qualification_results=results,
            repository="example/gwexpy-studio",
            default_branch="main",
        )


def test_native_groups_and_all_five_audit_are_distinct_kinds(tmp_path: Path) -> None:
    from scripts.verify_publication_groups import (
        audit_summary_bytes,
        publication_group_summary_bytes,
        read_publication_summary,
    )

    builds, results = _all_fixture(tmp_path)
    audit = json.loads(
        audit_summary_bytes(
            builds=builds,
            qualification_results=results,
            repository="example/gwexpy-studio",
            default_branch="main",
        )
    )
    assert audit["kind"] == "audit"
    assert audit["publication_group"] is None
    assert sum(len(row["qualification_results"]) for row in audit["targets"]) == 5
    read_publication_summary(_canonical(audit), kind="audit")

    for group_id, target_id in (
        ("ubuntu", "ubuntu24-x86_64"),
        ("debian", "debian13-x86_64"),
    ):
        summary = json.loads(
            publication_group_summary_bytes(
                group_id=group_id,
                builds={target_id: builds[target_id]},
                qualification_results={target_id: results[target_id]},
                repository="example/gwexpy-studio",
                default_branch="main",
            )
        )
        assert summary["kind"] == "publication-group"
        assert summary["publication_group"] == group_id
        assert [row["target_id"] for row in summary["targets"]] == [target_id]


def test_summary_reader_rejects_missing_nested_proof_fields(tmp_path: Path) -> None:
    from scripts.verify_publication_groups import (
        PublicationGroupError,
        publication_group_summary_bytes,
        read_publication_summary,
    )

    builds, results = _wsl_mac_fixture(tmp_path)
    valid_summary = json.loads(
        publication_group_summary_bytes(
            group_id="wsl2-mac",
            builds=builds,
            qualification_results=results,
            repository="example/gwexpy-studio",
            default_branch="main",
        )
    )
    summary = copy.deepcopy(valid_summary)
    del summary["targets"][0]["workflow"]
    with pytest.raises(PublicationGroupError, match="fields"):
        read_publication_summary(_canonical(summary))

    for build_id in (
        "not-a-build-id",
        "P-abcdef0-20261340-r1-a1",
    ):
        tampered = copy.deepcopy(valid_summary)
        tampered["targets"][0]["build_id"] = build_id
        with pytest.raises(PublicationGroupError, match="Build ID"):
            read_publication_summary(_canonical(tampered))


def test_group_rejects_expired_artifact_and_bool_run_id(tmp_path: Path) -> None:
    from scripts.verify_publication_groups import (
        PublicationGroupError,
        publication_group_summary_bytes,
    )

    builds, results = _wsl_mac_fixture(tmp_path)
    wsl = builds["wsl2-ubuntu24"]
    assert isinstance(wsl, dict)
    wsl["artifacts"][0]["expired"] = True
    with pytest.raises(PublicationGroupError, match="expired"):
        publication_group_summary_bytes(
            group_id="wsl2-mac",
            builds=builds,
            qualification_results=results,
            repository="example/gwexpy-studio",
            default_branch="main",
        )

    bool_id = tmp_path / "bool-id"
    bool_id.mkdir()
    builds, results = _wsl_mac_fixture(bool_id)
    wsl = builds["wsl2-ubuntu24"]
    assert isinstance(wsl, dict)
    wsl["run"]["id"] = True
    with pytest.raises(PublicationGroupError, match="positive integer"):
        publication_group_summary_bytes(
            group_id="wsl2-mac",
            builds=builds,
            qualification_results=results,
            repository="example/gwexpy-studio",
            default_branch="main",
        )


def test_group_rejects_wrong_host_and_untrusted_result_states(tmp_path: Path) -> None:
    from scripts.verify_platform_qualification import (
        qualification2_untrusted_failure_bytes,
    )
    from scripts.verify_publication_groups import (
        PublicationGroupError,
        publication_group_summary_bytes,
    )

    builds, results = _wsl_mac_fixture(tmp_path)

    wrong_host = copy.deepcopy(results)
    mac_result = json.loads(wrong_host["macos15-arm64"]["arm64"])
    mac_result["host"]["architecture"] = "x86_64"
    wrong_host["macos15-arm64"]["arm64"] = _canonical(mac_result)
    with pytest.raises(PublicationGroupError, match="identity or status"):
        publication_group_summary_bytes(
            group_id="wsl2-mac",
            builds=builds,
            qualification_results=wrong_host,
            repository="example/gwexpy-studio",
            default_branch="main",
        )

    false_result = copy.deepcopy(results)
    wsl_result = json.loads(false_result["wsl2-ubuntu24"]["x86_64"])
    owner_name = next(iter(wsl_result["owner_confirmations"]))
    wsl_result["owner_confirmations"][owner_name] = False
    wsl_result["status"] = "failed"
    false_result["wsl2-ubuntu24"]["x86_64"] = _canonical(wsl_result)
    with pytest.raises(PublicationGroupError, match="identity or status"):
        publication_group_summary_bytes(
            group_id="wsl2-mac",
            builds=builds,
            qualification_results=false_result,
            repository="example/gwexpy-studio",
            default_branch="main",
        )

    null_result = copy.deepcopy(results)
    null_result["wsl2-ubuntu24"]["x86_64"] = qualification2_untrusted_failure_bytes(
        stage="preflight", error_code="interrupted", cleanup=None
    )
    with pytest.raises(PublicationGroupError, match="identity or status"):
        publication_group_summary_bytes(
            group_id="wsl2-mac",
            builds=builds,
            qualification_results=null_result,
            repository="example/gwexpy-studio",
            default_branch="main",
        )

    old_result = copy.deepcopy(results)
    old_document = json.loads(old_result["macos15-arm64"]["arm64"])
    from scripts.verify_platform_qualification import (
        qualification_result_bytes,
        required_checks,
    )

    old_result["macos15-arm64"]["arm64"] = qualification_result_bytes(
        target_id="macos15-arm64",
        architecture="arm64",
        build_id=old_document["build_id"],
        source_sha=old_document["source_sha"],
        wheel_sha256=old_document["wheel_sha256"],
        host={
            "architecture": "arm64",
            "macos_version": "15.7.1",
            "python_version": "3.12.14",
        },
        checks={name: True for name in required_checks("macos15-arm64")},
    )
    with pytest.raises(PublicationGroupError, match="identity or status"):
        publication_group_summary_bytes(
            group_id="wsl2-mac",
            builds=builds,
            qualification_results=old_result,
            repository="example/gwexpy-studio",
            default_branch="main",
        )


def test_group_rejects_schema_three_resolution_bytes(tmp_path: Path) -> None:
    from scripts.verify_publication_groups import (
        PublicationGroupError,
        publication_group_summary_bytes,
    )

    builds, results = _wsl_mac_fixture(tmp_path)
    release = tmp_path / "wsl2-ubuntu24-release"
    archive_path = next(release.glob("*.zip"))
    with zipfile.ZipFile(archive_path) as archive:
        resolution_name = next(
            name
            for name in archive.namelist()
            if name.endswith("resolution-ubuntu24-x86_64.json")
        )
        resolution = json.loads(archive.read(resolution_name))
    resolution["schema"] = 3
    _rewrite_release_member(
        release,
        "resolution-ubuntu24-x86_64.json",
        _canonical(resolution),
    )
    wsl = builds["wsl2-ubuntu24"]
    assert isinstance(wsl, dict)
    wrapper = wsl["release_wrapper"]
    assert isinstance(wrapper, Path)
    _wrapper(wrapper, release)
    wsl["artifacts"][0]["digest"] = f"sha256:{_sha(wrapper.read_bytes())}"
    with pytest.raises(PublicationGroupError, match="schema-4|verification"):
        publication_group_summary_bytes(
            group_id="wsl2-mac",
            builds=builds,
            qualification_results=results,
            repository="example/gwexpy-studio",
            default_branch="main",
        )


@pytest.mark.parametrize(
    "mutation",
    (
        "missing-target",
        "extra-target",
        "missing-result",
        "extra-result",
        "duplicate-artifact",
        "alias-artifact",
    ),
)
def test_group_rejects_incomplete_or_aliased_inputs(
    tmp_path: Path, mutation: str
) -> None:
    from scripts.verify_publication_groups import (
        PublicationGroupError,
        publication_group_summary_bytes,
    )

    builds, results = _wsl_mac_fixture(tmp_path)
    builds = copy.deepcopy(builds)
    results = copy.deepcopy(results)
    if mutation == "missing-target":
        del builds["macos15-arm64"]
        del results["macos15-arm64"]
    elif mutation == "extra-target":
        builds["ubuntu24-x86_64"] = builds["wsl2-ubuntu24"]
        results["ubuntu24-x86_64"] = results["wsl2-ubuntu24"]
    elif mutation == "missing-result":
        del results["wsl2-ubuntu24"]["aarch64"]
    elif mutation == "extra-result":
        results["wsl2-ubuntu24"]["unexpected"] = results["wsl2-ubuntu24"][
            "x86_64"
        ]
    elif mutation == "duplicate-artifact":
        wsl = builds["wsl2-ubuntu24"]
        assert isinstance(wsl, dict)
        wsl["artifacts"].append(dict(wsl["artifacts"][0]))
    else:
        wsl = builds["wsl2-ubuntu24"]
        assert isinstance(wsl, dict)
        wsl["artifacts"][0]["name"] = "trial-bundle-macos15-arm64-123456-a1"

    with pytest.raises(PublicationGroupError):
        publication_group_summary_bytes(
            group_id="wsl2-mac",
            builds=builds,
            qualification_results=results,
            repository="example/gwexpy-studio",
            default_branch="main",
        )


@pytest.mark.parametrize(
    "field", ("repository", "head_branch", "run_attempt", "head_sha")
)
def test_group_rejects_untrusted_build_linkage(tmp_path: Path, field: str) -> None:
    from scripts.verify_publication_groups import (
        PublicationGroupError,
        publication_group_summary_bytes,
    )

    builds, results = _wsl_mac_fixture(tmp_path)
    wsl = builds["wsl2-ubuntu24"]
    assert isinstance(wsl, dict)
    if field == "repository":
        wsl["run"]["repository"] = {"full_name": "attacker/repository"}
    elif field == "head_branch":
        wsl["run"]["head_branch"] = "release"
    elif field == "head_sha":
        wsl["run"]["head_sha"] = "b" * 40
    else:
        wsl["run"]["run_attempt"] = 2
    with pytest.raises(PublicationGroupError):
        publication_group_summary_bytes(
            group_id="wsl2-mac",
            builds=builds,
            qualification_results=results,
            repository="example/gwexpy-studio",
            default_branch="main",
        )


def test_cli_rejects_duplicate_or_malformed_descriptors_and_never_overwrites(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from scripts.verify_publication_groups import main

    fixture_path = tmp_path / "fixture"
    fixture_path.mkdir()
    builds, results = _wsl_mac_fixture(fixture_path)
    descriptor = _cli_descriptor(tmp_path / "cli", builds, results)
    output = tmp_path / "cli" / "publication-summary.json"
    args = [
        "--input",
        str(descriptor),
        "--output",
        str(output),
        "--publication-group",
        "wsl2-mac",
    ]
    assert main(args) == 0
    summary_before = output.read_bytes()
    sidecar_before = output.with_suffix(".json.sha256").read_bytes()
    assert main(args) == 1
    assert output.read_bytes() == summary_before
    assert output.with_suffix(".json.sha256").read_bytes() == sidecar_before

    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text(
        '{"repository":"example/gwexpy-studio","repository":"other",'
        '"default_branch":"main","builds":{},"qualification_results":{}}',
        encoding="utf-8",
    )
    duplicate_output = tmp_path / "duplicate-summary.json"
    assert main(
        [
            "--input",
            str(duplicate),
            "--output",
            str(duplicate_output),
            "--publication-group",
            "wsl2-mac",
        ]
    ) == 1
    assert not duplicate_output.exists()

    malformed = json.loads(descriptor.read_text(encoding="utf-8"))
    malformed["qualification_results"]["wsl2-ubuntu24"]["x86_64"] = 42
    malformed_path = tmp_path / "malformed.json"
    malformed_path.write_bytes(_canonical(malformed))
    malformed_output = tmp_path / "malformed-summary.json"
    assert main(
        [
            "--input",
            str(malformed_path),
            "--output",
            str(malformed_output),
            "--publication-group",
            "wsl2-mac",
        ]
    ) == 1
    assert not malformed_output.exists()

    invalid_utf8 = tmp_path / "invalid-utf8.json"
    invalid_utf8.write_bytes(b"\xff\xfe\xfa")
    invalid_utf8_output = tmp_path / "invalid-utf8-summary.json"
    assert main(
        [
            "--input",
            str(invalid_utf8),
            "--output",
            str(invalid_utf8_output),
            "--publication-group",
            "wsl2-mac",
        ]
    ) == 1
    assert not invalid_utf8_output.exists()
    captured = capsys.readouterr()
    assert "Traceback" not in captured.out + captured.err
