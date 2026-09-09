"""Contracts for the immutable M2 trial asset bundle."""

from __future__ import annotations

import hashlib
import importlib
import json
import zipfile
from base64 import urlsafe_b64encode
from pathlib import Path

import pytest

from scripts.release_source_manifest import ManifestEntry, ReleaseSourceManifest

pytestmark = pytest.mark.unit

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]

_M2_RUNTIME_REQUIREMENTS = (
    "PySide6-Essentials==6.11.2",
    "gwexpy==0.2.0",
    "gwpy<5.0.0,>=4.0.0",
    "numpy<3.0.0,>=2.0.0",
    "scipy<2.0.0,>=1.15.0",
    "astropy<9.0.0,>=7.0.0",
    "matplotlib<4.0.0,>=3.10.0",
)
_M2_DEV_REQUIREMENTS = (
    "setuptools>=68",
    "pytest",
    "pytest-cov",
    "pytest-timeout",
    "ruff",
    "mypy",
    "jsonschema",
    "packaging",
)


def _bundle():
    return importlib.import_module("scripts.assemble_trial_bundle")


def _verifier():
    return importlib.import_module("scripts.verify_trial_bundle")


def _canonical_json(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _os_runtime(architecture: str) -> dict[str, object]:
    """Return strict direct Qt GL runtime evidence for one fixture host."""
    dpkg_architecture = {"x86_64": "amd64", "aarch64": "arm64"}[architecture]
    return {
        "manager": "dpkg",
        "packages": [
            {
                "architecture": dpkg_architecture,
                "name": "libegl1",
                "status": "installed",
                "version": "1.7.0-1build1",
            },
            {
                "architecture": dpkg_architecture,
                "name": "libgl1",
                "status": "installed",
                "version": "1.7.0-1build1",
            },
        ],
        "required_libraries": ["libEGL.so.1", "libGL.so.1"],
    }


def _wheel_metadata(version: str) -> bytes:
    """Render the dependency metadata accepted by the fixed M2 wheel policy."""
    lines = [
        "Metadata-Version: 2.4",
        "Name: gwexpy-studio",
        f"Version: {version}",
        "Requires-Python: <3.13,>=3.12",
        "License-File: LICENSE",
        *(f"Requires-Dist: {value}" for value in _M2_RUNTIME_REQUIREMENTS),
        "Provides-Extra: dev",
        *(
            f'Requires-Dist: {value}; extra == "dev"'
            for value in _M2_DEV_REQUIREMENTS
        ),
        "Dynamic: license-file",
        "",
    ]
    return "\n".join(lines).encode("utf-8")


def _manifest_entry(path: str, content: bytes) -> ManifestEntry:
    return ManifestEntry(
        path=path, file_type="file", mode="0644", sha256=_sha256(content)
    )


def _record_bytes(members: dict[str, bytes], record_name: str) -> bytes:
    """Render a complete wheel RECORD for the supplied non-RECORD members."""
    rows = []
    for name, content in members.items():
        digest = urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b"=")
        rows.append(f"{name},sha256={digest.decode('ascii')},{len(content)}")
    return ("\n".join((*rows, f"{record_name},,")) + "\n").encode("utf-8")


def _write_wheel_members(wheel: Path, members: dict[str, bytes]) -> None:
    """Write one fixture wheel while regenerating its mandatory RECORD member."""
    record_name = next(name for name in members if name.endswith(".dist-info/RECORD"))
    members[record_name] = _record_bytes(
        {name: content for name, content in members.items() if name != record_name},
        record_name,
    )
    with zipfile.ZipFile(wheel, "w") as archive:
        for name, content in members.items():
            archive.writestr(name, content)


def _write_trial_inputs(tmp_path: Path) -> dict[str, Path]:
    """Create minimal, mutually bound M2 inputs without a network install."""
    source_version = b'__version__ = "0.1.0a1"\n'
    capability_policy = _canonical_json(
        {
            "entries": [
                {
                    "datatype": "TimeSeries",
                    "direction": "read",
                    "format": "csv",
                    "tier": "A",
                }
            ],
            "schema_version": 1,
        }
    )
    source_license = b"fixture license\n"
    quick_start = tmp_path / "Quick-Start.md"
    quick_start.write_text("# Quick start\n", encoding="utf-8")
    quick_start_ja = tmp_path / "Quick-Start.ja.md"
    quick_start_ja.write_text("# クイックスタート\n", encoding="utf-8")
    feedback = tmp_path / "Feedback.ja.md"
    feedback.write_text("# フィードバック\n", encoding="utf-8")
    source_manifest = tmp_path / "SOURCE-MANIFEST.json"
    source = ReleaseSourceManifest(
        entries=tuple(
            sorted(
                (
                    _manifest_entry("LICENSE", source_license),
                    _manifest_entry("docs/Quick-Start.md", quick_start.read_bytes()),
                    _manifest_entry(
                        "docs/Quick-Start.ja.md", quick_start_ja.read_bytes()
                    ),
                    _manifest_entry("docs/Feedback.ja.md", feedback.read_bytes()),
                    _manifest_entry(
                        "packaging/trial-io-capabilities.json", capability_policy
                    ),
                    _manifest_entry("src/gwexpy_studio/_version.py", source_version),
                ),
                key=lambda entry: entry.path.encode(),
            )
        )
    )
    source_manifest.write_bytes(source.to_bytes())
    source_sha = "abcdef0123456789abcdef0123456789abcdef01"
    build_id = "P-abcdef0-20260907-r1-a1"
    version = "0.1.0a1+trial.p.gabcdef0.20260907.r1.a1"
    identity = {
        "build_id": build_id,
        "source_manifest_sha256": _sha256(source_manifest.read_bytes()),
        "source_sha": source_sha,
        "version": version,
    }
    wheel = tmp_path / f"gwexpy_studio-{version}-py3-none-any.whl"
    generated = {
        "src/gwexpy_studio/_version.py": f'__version__ = "{version}"\n'.encode(),
        "src/gwexpy_studio/assets/trial-build.json": _canonical_json(
            {**identity, "schema": 1}
        ),
        "src/gwexpy_studio/assets/io-capabilities.json": capability_policy,
    }
    dist_info = f"gwexpy_studio-{version}.dist-info"
    members = {
        **{path.removeprefix("src/"): content for path, content in generated.items()},
        f"{dist_info}/METADATA": _wheel_metadata(version),
        f"{dist_info}/WHEEL": (
            b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
        ),
        f"{dist_info}/entry_points.txt": (
            b"[gui_scripts]\ngwexpy-studio = gwexpy_studio.ui.app:main\n"
        ),
        f"{dist_info}/top_level.txt": b"gwexpy_studio\n",
        f"{dist_info}/licenses/LICENSE": source_license,
        f"{dist_info}/RECORD": b"",
    }
    _write_wheel_members(wheel, members)
    staging_entries = {entry.path: entry for entry in source.entries}
    staging_entries.update(
        {path: _manifest_entry(path, content) for path, content in generated.items()}
    )
    staging = ReleaseSourceManifest(
        entries=tuple(
            sorted(staging_entries.values(), key=lambda entry: entry.path.encode())
        )
    )
    staging_manifest = tmp_path / "STAGING-MANIFEST.json"
    staging_manifest.write_bytes(staging.to_bytes())
    preliminary_trial_manifest = tmp_path / "preliminary-TRIAL-MANIFEST.json"
    preliminary_trial_manifest.write_bytes(
        _canonical_json(
            {
                **identity,
                "generated_files": [
                    {"path": path, "sha256": _sha256(content)}
                    for path, content in generated.items()
                ],
                "schema": 1,
                "staging_manifest_sha256": _sha256(staging_manifest.read_bytes()),
                "wheel_filename": wheel.name,
                "wheel_sha256": _sha256(wheel.read_bytes()),
            }
        )
    )
    inputs = {
        "wheel": wheel,
        "source_manifest": source_manifest,
        "staging_manifest": staging_manifest,
        "trial_manifest": preliminary_trial_manifest,
        "quick_start": quick_start,
        "quick_start_ja": quick_start_ja,
        "feedback": feedback,
    }
    for architecture in ("x86_64", "aarch64"):
        constraints = tmp_path / f"constraints-ubuntu24-{architecture}.txt"
        constraints.write_text("example-runtime==1.2.3\n", encoding="utf-8")
        checks = {
            name: True
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
        runtime_artifacts = [
            {
                "filename": "example_runtime-1.2.3-py3-none-any.whl",
                "name": "example-runtime",
                "sha256": "a" * 64,
                "version": "1.2.3",
            }
        ]
        phase_artifacts = [
            *runtime_artifacts,
            {
                "filename": wheel.name,
                "name": "gwexpy-studio",
                "sha256": _sha256(wheel.read_bytes()),
                "version": version,
            },
        ]
        installed = [
            {"name": artifact["name"], "version": artifact["version"]}
            for artifact in phase_artifacts
        ]
        phase_one = {
            "artifacts": phase_artifacts,
            "inspect": {"installed": installed},
            "report": {"artifacts": phase_artifacts},
        }
        phase_one["pip_inspect_sha256"] = _sha256(_canonical_json(phase_one["inspect"]))
        phase_one["pip_report_sha256"] = _sha256(_canonical_json(phase_one["report"]))
        phase_two = {
            "artifacts": phase_artifacts,
            "inspect": {"installed": installed},
            "report": {"artifacts": phase_artifacts},
        }
        phase_two["pip_inspect_sha256"] = _sha256(_canonical_json(phase_two["inspect"]))
        phase_two["pip_report_sha256"] = _sha256(_canonical_json(phase_two["report"]))
        resolution = tmp_path / f"resolution-ubuntu24-{architecture}.json"
        resolution.write_bytes(
            _canonical_json(
                {
                    "architecture": architecture,
                    "build_id": build_id,
                    "constraints_sha256": _sha256(constraints.read_bytes()),
                    "glibc_version": "2.39",
                    "os_runtime": _os_runtime(architecture),
                    "os_id": "ubuntu",
                    "os_version": "24.04",
                    "phase_one": phase_one,
                    "phase_two": phase_two,
                    "pip_version": "25.0.1",
                    "python_version": "3.12.12",
                    "runtime_artifacts": runtime_artifacts,
                    "schema": 2,
                    "source_manifest_sha256": identity["source_manifest_sha256"],
                    "source_sha": source_sha,
                    "staging_manifest_sha256": _sha256(staging_manifest.read_bytes()),
                    "technical_gate": {
                        "architecture": architecture,
                        "checks": checks,
                        "installed": identity,
                        "python_version": "3.12.12",
                        "schema": 2,
                        "status": "passed",
                    },
                    "version": version,
                    "wheel": {
                        "filename": wheel.name,
                        "sha256": _sha256(wheel.read_bytes()),
                    },
                }
            )
        )
        inputs[f"constraints_{architecture}"] = constraints
        inputs[f"resolution_{architecture}"] = resolution
    return inputs


def _assemble(inputs: dict[str, Path], output: Path) -> None:
    _bundle().assemble_trial_bundle(
        output_directory=output,
        wheel=inputs["wheel"],
        preliminary_trial_manifest=inputs["trial_manifest"],
        source_manifest=inputs["source_manifest"],
        staging_manifest=inputs["staging_manifest"],
        constraints_x86_64=inputs["constraints_x86_64"],
        resolution_x86_64=inputs["resolution_x86_64"],
        constraints_aarch64=inputs["constraints_aarch64"],
        resolution_aarch64=inputs["resolution_aarch64"],
        quick_start=inputs["quick_start"],
        quick_start_ja=inputs["quick_start_ja"],
        feedback_ja=inputs["feedback"],
        repository="example/gwexpy-studio",
        build_workflow_path=".github/workflows/build-trial-wheel.yml",
        build_run_id=123456,
        build_run_number=1,
        build_run_attempt=1,
    )


def _refresh_checksums(bundle: Path) -> None:
    files = {
        path.name: path.read_bytes()
        for path in bundle.iterdir()
        if path.name != "SHA256SUMS"
    }
    checksums = b"".join(
        f"{_sha256(files[name])}  {name}\n".encode("ascii")
        for name in sorted(files, key=lambda name: name.encode())
    )
    (bundle / "SHA256SUMS").write_bytes(checksums)


def _rewrite_wheel_member(wheel: Path, member: str, content: bytes) -> None:
    with zipfile.ZipFile(wheel) as archive:
        members = {name: archive.read(name) for name in archive.namelist()}
    members[member] = content
    _write_wheel_members(wheel, members)


def _refresh_phase_digests(resolution: dict[str, object]) -> None:
    for phase_name in ("phase_one", "phase_two"):
        phase = resolution[phase_name]
        assert isinstance(phase, dict)
        phase["report"] = {"artifacts": phase["artifacts"]}
        phase["pip_report_sha256"] = _sha256(_canonical_json(phase["report"]))
        phase["pip_inspect_sha256"] = _sha256(_canonical_json(phase["inspect"]))


def _refresh_tampered_trial_bindings(inputs: dict[str, Path]) -> None:
    """Make fixture sidecars agree with a changed wheel, except semantic content."""
    wheel = inputs["wheel"]
    with zipfile.ZipFile(wheel) as archive:
        generated = {
            "src/gwexpy_studio/_version.py": archive.read("gwexpy_studio/_version.py"),
            "src/gwexpy_studio/assets/trial-build.json": archive.read(
                "gwexpy_studio/assets/trial-build.json"
            ),
            "src/gwexpy_studio/assets/io-capabilities.json": archive.read(
                "gwexpy_studio/assets/io-capabilities.json"
            ),
        }
    staging_path = inputs["staging_manifest"]
    staging = json.loads(staging_path.read_text(encoding="utf-8"))
    for entry in staging["entries"]:
        if entry["path"] in generated:
            entry["sha256"] = _sha256(generated[entry["path"]])
    staging_path.write_bytes(_canonical_json(staging))
    trial_path = inputs["trial_manifest"]
    trial = json.loads(trial_path.read_text(encoding="utf-8"))
    for entry in trial["generated_files"]:
        entry["sha256"] = _sha256(generated[entry["path"]])
    trial["staging_manifest_sha256"] = _sha256(staging_path.read_bytes())
    trial["wheel_sha256"] = _sha256(wheel.read_bytes())
    trial_path.write_bytes(_canonical_json(trial))
    for architecture in ("x86_64", "aarch64"):
        resolution_path = inputs[f"resolution_{architecture}"]
        resolution = json.loads(resolution_path.read_text(encoding="utf-8"))
        resolution["staging_manifest_sha256"] = trial["staging_manifest_sha256"]
        resolution["wheel"]["sha256"] = trial["wheel_sha256"]
        for phase in (resolution["phase_one"], resolution["phase_two"]):
            studio = next(
                artifact
                for artifact in phase["artifacts"]
                if artifact["name"] == "gwexpy-studio"
            )
            studio["filename"] = trial["wheel_filename"]
            studio["sha256"] = trial["wheel_sha256"]
            studio["version"] = trial["version"]
        _refresh_phase_digests(resolution)
        resolution_path.write_bytes(_canonical_json(resolution))


def _rebind_final_bundle_wheel(bundle: Path, wheel: Path) -> None:
    """Recompute every detached fixture binding after a controlled wheel rewrite."""
    trial_path = bundle / "TRIAL-MANIFEST.json"
    trial = json.loads(trial_path.read_text(encoding="utf-8"))
    wheel_sha256 = _sha256(wheel.read_bytes())
    wheel_filename = wheel.name
    trial["wheel"] = {"filename": wheel_filename, "sha256": wheel_sha256}
    architectures = trial["architectures"]
    assert isinstance(architectures, dict)
    for architecture in ("x86_64", "aarch64"):
        record = architectures[architecture]
        assert isinstance(record, dict)
        resolution_path = bundle / f"resolution-ubuntu24-{architecture}.json"
        resolution = json.loads(resolution_path.read_text(encoding="utf-8"))
        resolution["wheel"] = {"filename": wheel_filename, "sha256": wheel_sha256}
        for phase_name in ("phase_one", "phase_two"):
            phase = resolution[phase_name]
            assert isinstance(phase, dict)
            studio = next(
                artifact
                for artifact in phase["artifacts"]
                if artifact["name"] == "gwexpy-studio"
            )
            studio["filename"] = wheel_filename
            studio["sha256"] = wheel_sha256
        _refresh_phase_digests(resolution)
        resolution_path.write_bytes(_canonical_json(resolution))
        resolution_record = record["resolution"]
        assert isinstance(resolution_record, dict)
        resolution_record["sha256"] = _sha256(resolution_path.read_bytes())

    build = trial["build"]
    source = trial["source"]
    staging = trial["staging"]
    assert isinstance(build, dict)
    assert isinstance(source, dict)
    assert isinstance(staging, dict)
    trial["preliminary_trial_manifest_sha256"] = _sha256(
        _canonical_json(
            {
                "build_id": build["id"],
                "generated_files": staging["generated_files"],
                "schema": 1,
                "source_manifest_sha256": source["manifest_sha256"],
                "source_sha": build["source_sha"],
                "staging_manifest_sha256": staging["manifest_sha256"],
                "version": build["version"],
                "wheel_filename": wheel_filename,
                "wheel_sha256": wheel_sha256,
            }
        )
    )
    trial_path.write_bytes(_canonical_json(trial))
    _refresh_checksums(bundle)


def test_assemble_and_verify_trial_bundle_round_trip(tmp_path: Path) -> None:
    """The public bundle has one fixed, self-checking trial asset set."""
    inputs = _write_trial_inputs(tmp_path)
    output = tmp_path / "bundle"

    _assemble(inputs, output)

    expected = {
        inputs["wheel"].name,
        "constraints-ubuntu24-x86_64.txt",
        "constraints-ubuntu24-aarch64.txt",
        "resolution-ubuntu24-x86_64.json",
        "resolution-ubuntu24-aarch64.json",
        "SOURCE-MANIFEST.json",
        "TRIAL-MANIFEST.json",
        "SHA256SUMS",
        "Quick-Start.md",
        "Quick-Start.ja.md",
        "Feedback.ja.md",
    }
    assert {path.name for path in output.iterdir()} == expected

    verified = _verifier().verify_trial_bundle(output)

    assert verified["build"]["id"] == "P-abcdef0-20260907-r1-a1"
    assert verified["build"]["source_sha"] == "abcdef0123456789abcdef0123456789abcdef01"


def test_assemble_rejects_a_resolution_without_strict_qt_gl_runtime(
    tmp_path: Path,
) -> None:
    """The final bundle rejects missing or cross-architecture OS runtime evidence."""
    inputs = _write_trial_inputs(tmp_path)
    resolution = inputs["resolution_x86_64"]
    document = json.loads(resolution.read_text(encoding="utf-8"))
    os_runtime = document["os_runtime"]
    assert isinstance(os_runtime, dict)
    packages = os_runtime["packages"]
    assert isinstance(packages, list)
    packages[0]["architecture"] = "arm64"
    resolution.write_bytes(_canonical_json(document))

    with pytest.raises(_bundle().TrialBundleError, match="OS runtime"):
        _assemble(inputs, tmp_path / "bundle")


def test_verifier_rejects_a_rebound_bundle_with_noninstalled_qt_gl_runtime(
    tmp_path: Path,
) -> None:
    """Rewritten hashes cannot legitimize a package that was not installed."""
    inputs = _write_trial_inputs(tmp_path)
    output = tmp_path / "bundle"
    _assemble(inputs, output)
    resolution_path = output / "resolution-ubuntu24-x86_64.json"
    resolution = json.loads(resolution_path.read_text(encoding="utf-8"))
    os_runtime = resolution["os_runtime"]
    assert isinstance(os_runtime, dict)
    packages = os_runtime["packages"]
    assert isinstance(packages, list)
    packages[1]["status"] = "not-installed"
    resolution_path.write_bytes(_canonical_json(resolution))

    trial_path = output / "TRIAL-MANIFEST.json"
    trial = json.loads(trial_path.read_text(encoding="utf-8"))
    architectures = trial["architectures"]
    assert isinstance(architectures, dict)
    x86_64 = architectures["x86_64"]
    assert isinstance(x86_64, dict)
    resolution_record = x86_64["resolution"]
    assert isinstance(resolution_record, dict)
    resolution_record["sha256"] = _sha256(resolution_path.read_bytes())
    trial_path.write_bytes(_canonical_json(trial))
    _refresh_checksums(output)

    with pytest.raises(_bundle().TrialBundleError, match="OS runtime"):
        _verifier().verify_trial_bundle(output)


def test_assemble_never_replaces_a_preexisting_bundle(tmp_path: Path) -> None:
    """A same-name retry cannot replace evidence that was already published."""
    inputs = _write_trial_inputs(tmp_path)
    output = tmp_path / "bundle"
    _assemble(inputs, output)

    with pytest.raises(_bundle().TrialBundleError, match="output already exists"):
        _assemble(inputs, output)

    assert _verifier().verify_trial_bundle(output)["schema"] == 3


def test_trial_quick_starts_describe_the_initial_x86_release_path() -> None:
    """The instructions start at the two release assets and stay on Ubuntu x86."""
    english = (REPOSITORY_ROOT / "docs" / "Quick-Start.md").read_text(encoding="utf-8")
    japanese = (REPOSITORY_ROOT / "docs" / "Quick-Start.ja.md").read_text(
        encoding="utf-8"
    )

    for document in (english, japanese):
        assert "conda create -n gwexpy-studio python=3.12" in document
        assert "ctypes.CDLL" in document
        assert "libEGL.so.1" in document
        assert "libGL.so.1" in document
        assert "libegl1 libgl1" in document
        assert "sudo apt-get update" in document
        assert "sha256sum -c SHA256SUMS" in document
        assert "--only-binary=:all:" in document
        assert "constraints-ubuntu24-x86_64.txt" in document
        assert "constraints-ubuntu24-aarch64.txt" not in document
        assert ".zip.sha256" in document
        assert "WSL2" not in document
        assert "gwexpy-studio" in document
        assert "git clone" not in document
        assert "pip install -e" not in document


def test_trial_readiness_records_qt_gl_baselines_and_m1_workflow_scope() -> None:
    """The human trials distinguish an OS runtime fix from GUI behavior."""
    readiness = (
        REPOSITORY_ROOT / "docs" / "release" / "0.1.0a1-trial-readiness.md"
    ).read_text(encoding="utf-8")
    roadmap = (REPOSITORY_ROOT / "ROADMAP.md").read_text(encoding="utf-8")

    assert "included in the M1 public source" in readiness
    assert "first builds and qualifies" in readiness
    assert "libEGL.so.1" in readiness
    assert "libGL.so.1" in readiness
    assert "before remediation" in readiness
    assert "WSLg" in readiness
    assert "M1 public source" in roadmap
    assert "M2" in roadmap
    assert "初回buildとqualification" in roadmap


def test_verifier_rejects_a_wheel_changed_after_assembly(tmp_path: Path) -> None:
    """A replacement wheel cannot be legitimized by the original checksum file."""
    inputs = _write_trial_inputs(tmp_path)
    output = tmp_path / "bundle"
    _assemble(inputs, output)
    wheel = output / inputs["wheel"].name
    wheel.write_bytes(wheel.read_bytes() + b"changed")

    with pytest.raises(_bundle().TrialBundleError, match="wheel digest"):
        _verifier().verify_trial_bundle(output)


def test_assemble_rejects_an_unknown_wheel_package_member_after_rebinding(
    tmp_path: Path,
) -> None:
    """A rebuilt sidecar set cannot legitimize code absent from staged source."""
    inputs = _write_trial_inputs(tmp_path)
    _rewrite_wheel_member(
        inputs["wheel"], "gwexpy_studio/backdoor.py", b"raise RuntimeError\n"
    )
    _refresh_tampered_trial_bindings(inputs)

    with pytest.raises(_bundle().TrialBundleError, match="payload"):
        _assemble(inputs, tmp_path / "bundle")


def test_verifier_rejects_unstaged_package_member_after_all_bindings_rebound(
    tmp_path: Path,
) -> None:
    """Final verification rejects code even after every detached hash is recomputed."""
    inputs = _write_trial_inputs(tmp_path)
    output = tmp_path / "bundle"
    _assemble(inputs, output)
    wheel = output / inputs["wheel"].name
    _rewrite_wheel_member(
        wheel, "gwexpy_studio/backdoor.py", b"raise RuntimeError\n"
    )
    _rebind_final_bundle_wheel(output, wheel)

    with pytest.raises(_bundle().TrialBundleError, match="payload"):
        _verifier().verify_trial_bundle(output)


def test_verifier_rejects_injected_metadata_dependency_after_all_bindings_rebound(
    tmp_path: Path,
) -> None:
    """Final verification cannot let a rebuilt wheel add a resolver dependency."""
    inputs = _write_trial_inputs(tmp_path)
    output = tmp_path / "bundle"
    _assemble(inputs, output)
    wheel = output / inputs["wheel"].name
    with zipfile.ZipFile(wheel) as archive:
        metadata = next(
            name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
        )
        original = archive.read(metadata)
    _rewrite_wheel_member(
        wheel,
        metadata,
        original + b"Requires-Dist: injected-review-dependency\n",
    )
    _rebind_final_bundle_wheel(output, wheel)

    with pytest.raises(_bundle().TrialBundleError, match="metadata"):
        _verifier().verify_trial_bundle(output)


def test_verifier_rejects_a_substituted_quick_start_with_rewritten_checksums(
    tmp_path: Path,
) -> None:
    """The source manifest, rather than SHA256SUMS alone, binds user instructions."""
    inputs = _write_trial_inputs(tmp_path)
    output = tmp_path / "bundle"
    _assemble(inputs, output)
    quick_start = output / "Quick-Start.md"
    quick_start.write_text("# substituted\n", encoding="utf-8")
    trial_path = output / "TRIAL-MANIFEST.json"
    trial = json.loads(trial_path.read_text(encoding="utf-8"))
    trial["quick_start"]["en"]["sha256"] = _sha256(quick_start.read_bytes())
    trial_path.write_bytes(_canonical_json(trial))
    _refresh_checksums(output)

    with pytest.raises(_bundle().TrialBundleError, match="canonical source"):
        _verifier().verify_trial_bundle(output)


def test_assemble_rejects_a_resolution_with_a_mismatched_report_digest(
    tmp_path: Path,
) -> None:
    """The resolver's public report projection must match its recorded digest."""
    inputs = _write_trial_inputs(tmp_path)
    resolution = inputs["resolution_x86_64"]
    document = json.loads(resolution.read_text(encoding="utf-8"))
    document["phase_one"]["pip_report_sha256"] = "0" * 64
    resolution.write_bytes(_canonical_json(document))

    with pytest.raises(_bundle().TrialBundleError, match="report digest"):
        _assemble(inputs, tmp_path / "bundle")


def test_assemble_rejects_a_wheel_with_a_semantically_wrong_version_stamp(
    tmp_path: Path,
) -> None:
    """Generated files must express the same version and policy as final provenance."""
    inputs = _write_trial_inputs(tmp_path)
    _rewrite_wheel_member(
        inputs["wheel"],
        "gwexpy_studio/_version.py",
        b'__version__ = "0.1.0a1+trial.p.gabcdef0.20260907.r1.a1.changed"\n',
    )
    _refresh_tampered_trial_bindings(inputs)

    with pytest.raises(_bundle().TrialBundleError, match="generated trial files"):
        _assemble(inputs, tmp_path / "bundle")


def test_assemble_rejects_a_policy_not_bound_to_source_manifest(
    tmp_path: Path,
) -> None:
    """The capability policy must be the exact policy selected in source M."""
    inputs = _write_trial_inputs(tmp_path)
    policy = {
        "entries": [
            {
                "datatype": "TimeSeries",
                "direction": "read",
                "format": "csv",
                "tier": "A",
            }
        ],
        "schema_version": 1,
    }
    alternate_serialization = json.dumps(
        policy, ensure_ascii=True, sort_keys=True, indent=2
    ).encode("utf-8") + b"\n"
    _rewrite_wheel_member(
        inputs["wheel"],
        "gwexpy_studio/assets/io-capabilities.json",
        alternate_serialization,
    )
    _refresh_tampered_trial_bindings(inputs)

    with pytest.raises(_bundle().TrialBundleError, match="generated trial files"):
        _assemble(inputs, tmp_path / "bundle")


def test_assemble_rejects_different_phase_closures(tmp_path: Path) -> None:
    """The fresh constrained reinstall must preserve the resolved closure exactly."""
    inputs = _write_trial_inputs(tmp_path)
    resolution = inputs["resolution_aarch64"]
    document = json.loads(resolution.read_text(encoding="utf-8"))
    inspect = {"installed": [{"name": "unexpected", "version": "9.9.9"}]}
    document["phase_two"]["inspect"] = inspect
    document["phase_two"]["pip_inspect_sha256"] = _sha256(_canonical_json(inspect))
    resolution.write_bytes(_canonical_json(document))

    with pytest.raises(_bundle().TrialBundleError, match="phase closures"):
        _assemble(inputs, tmp_path / "bundle")


def test_assemble_rejects_runtime_artifacts_detached_from_the_phases(
    tmp_path: Path,
) -> None:
    """Constraints cannot describe a closure different from the installed phases."""
    inputs = _write_trial_inputs(tmp_path)
    architecture = "x86_64"
    constraints = inputs[f"constraints_{architecture}"]
    resolution = inputs[f"resolution_{architecture}"]
    document = json.loads(resolution.read_text(encoding="utf-8"))

    constraints.write_bytes(b"")
    document["runtime_artifacts"] = []
    document["constraints_sha256"] = _sha256(constraints.read_bytes())
    resolution.write_bytes(_canonical_json(document))

    with pytest.raises(_bundle().TrialBundleError, match="phase closures"):
        _assemble(inputs, tmp_path / "bundle")


def test_assemble_rejects_a_runtime_artifact_hash_detached_from_its_phase(
    tmp_path: Path,
) -> None:
    """The closure includes resolved artifact filenames and hashes, not just pins."""
    inputs = _write_trial_inputs(tmp_path)
    resolution = inputs["resolution_aarch64"]
    document = json.loads(resolution.read_text(encoding="utf-8"))
    document["runtime_artifacts"][0]["sha256"] = "b" * 64
    resolution.write_bytes(_canonical_json(document))

    with pytest.raises(_bundle().TrialBundleError, match="phase closures"):
        _assemble(inputs, tmp_path / "bundle")


def test_assemble_rejects_a_phase_studio_artifact_detached_from_the_wheel(
    tmp_path: Path,
) -> None:
    """The direct Studio entry in each resolver phase is the published wheel."""
    inputs = _write_trial_inputs(tmp_path)
    resolution = inputs["resolution_x86_64"]
    document = json.loads(resolution.read_text(encoding="utf-8"))
    for phase_name in ("phase_one", "phase_two"):
        phase = document[phase_name]
        studio = next(
            artifact
            for artifact in phase["artifacts"]
            if artifact["name"] == "gwexpy-studio"
        )
        studio["sha256"] = "b" * 64
    _refresh_phase_digests(document)
    resolution.write_bytes(_canonical_json(document))

    with pytest.raises(_bundle().TrialBundleError, match="phase closures"):
        _assemble(inputs, tmp_path / "bundle")
