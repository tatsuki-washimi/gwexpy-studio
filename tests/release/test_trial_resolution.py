"""Contracts for architecture-specific trial runtime-resolution evidence."""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import subprocess
import sys
import zipfile
from base64 import urlsafe_b64encode
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.release_source_manifest import (
    ManifestEntry,
    ReleaseSourceManifest,
    load_policy,
    manifest_digest,
    read_manifest,
)
from scripts.verify_public_source import scan_public_checkout

pytestmark = pytest.mark.unit

_TRIAL_CAPABILITY_POLICY = {
    "schema_version": 1,
    "entries": [
        {
            "datatype": "TimeSeries",
            "format": "csv",
            "direction": "read",
            "tier": "A",
        }
    ],
}
_FIXTURE_LICENSE = b"fixture license\n"
_ENTRY_POINTS = b"[gui_scripts]\ngwexpy-studio = gwexpy_studio.ui.app:main\n"
_TOP_LEVEL = b"gwexpy_studio\n"
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


def _resolution():
    return importlib.import_module("scripts.capture_trial_resolution")


def _gate():
    return importlib.import_module("scripts.run_trial_technical_gate")


def test_macos_resolution_platform_adapter_is_native_and_path_free() -> None:
    module = _resolution()

    record = module.macos_platform_record(
        machine="arm64",
        os_version="15.7.1",
        qt_platform="cocoa",
        qt_version="6.11.2",
        opengl_context=True,
    )

    assert record == {
        "architecture": "arm64",
        "id": "macos",
        "qt_opengl_context": True,
        "qt_platform": "cocoa",
        "qt_version": "6.11.2",
        "version": "15.7.1",
    }
    assert module._constraints_filename("arm64", "macos15-arm64") == (
        "constraints-macos15-arm64.txt"
    )
    assert module._resolution_filename("arm64", "macos15-arm64") == (
        "resolution-macos15-arm64.json"
    )


@pytest.mark.parametrize(
    ("machine", "version", "qt_platform"),
    [
        ("x86_64", "15.7.1", "cocoa"),
        ("arm64", "14.7.1", "cocoa"),
        ("arm64", "15.7.1", "offscreen"),
    ],
)
def test_macos_resolution_platform_adapter_rejects_wrong_host(
    machine: str, version: str, qt_platform: str
) -> None:
    with pytest.raises(_resolution().ResolutionError):
        _resolution().macos_platform_record(
            machine=machine,
            os_version=version,
            qt_platform=qt_platform,
            qt_version="6.11.2",
            opengl_context=True,
        )


def test_macos_resolution_projection_uses_schema3_without_linux_runtime(
    trial_artifact: tuple[Path, Path, Path, dict[str, str]],
) -> None:
    wheel, trial_manifest, source_manifest, _identity = trial_artifact
    artifact = _resolution().load_trial_artifact(
        wheel=wheel,
        trial_manifest=trial_manifest,
        source_manifest=source_manifest,
    )
    report = {
        "install": [
            {
                "download_info": {
                    "archive_info": {"hashes": {"sha256": artifact.wheel_sha256}},
                    "url": f"file://{wheel}",
                },
                "is_direct": True,
                "is_yanked": False,
                "metadata": {"name": "gwexpy-studio", "version": artifact.version},
            }
        ],
        "pip_version": "25.3",
        "version": "1",
    }
    inspect = {
        "installed": [
            {"metadata": {"name": "gwexpy-studio", "version": artifact.version}}
        ],
        "pip_version": "25.3",
        "version": "1",
    }
    gate = {
        "architecture": "arm64",
        "checks": {name: True for name in _gate()._CHECK_NAMES},
        "installed": {
            "build_id": artifact.build_id,
            "source_sha": artifact.source_sha,
            "version": artifact.version,
        },
        "python_version": "3.12.12",
        "schema": 2,
        "status": "passed",
    }

    projection = _resolution().build_macos_resolution_projection(
        artifact=artifact,
        platform_record=_resolution().macos_platform_record(
            machine="arm64",
            os_version="15.7.1",
            qt_platform="cocoa",
            qt_version="6.11.2",
            opengl_context=True,
        ),
        python_version="3.12.12",
        pip_version="25.3",
        phase_one_report=report,
        phase_one_inspect=inspect,
        phase_two_report=report,
        phase_two_inspect=inspect,
        technical_gate=gate,
    )

    assert projection["schema"] == 3
    assert projection["architecture"] == "arm64"
    assert projection["platform"]["qt_platform"] == "cocoa"
    assert "glibc_version" not in projection
    assert "os_runtime" not in projection


def _os_runtime(machine: str = "x86_64") -> dict[str, object]:
    """Return the direct, path-free Qt GL runtime record for a fixture host."""
    architecture = {"x86_64": "amd64", "aarch64": "arm64"}[machine]
    return {
        "manager": "dpkg",
        "packages": [
            {
                "architecture": architecture,
                "name": "libegl1",
                "status": "installed",
                "version": "1.7.0-1build1",
            },
            {
                "architecture": architecture,
                "name": "libgl1",
                "status": "installed",
                "version": "1.7.0-1build1",
            },
        ],
        "required_libraries": ["libEGL.so.1", "libGL.so.1"],
    }


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


def _trial_capability_policy_bytes() -> bytes:
    return _canonical_json(_TRIAL_CAPABILITY_POLICY)


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _wheel_metadata(version: str) -> bytes:
    """Render the full static metadata allowed for the M2 trial wheel."""
    lines = [
        "Metadata-Version: 2.4",
        "Name: gwexpy-studio",
        f"Version: {version}",
        "Requires-Python: <3.13,>=3.12",
        "License-File: LICENSE",
        *(f"Requires-Dist: {value}" for value in _M2_RUNTIME_REQUIREMENTS),
        "Provides-Extra: dev",
        *(f'Requires-Dist: {value}; extra == "dev"' for value in _M2_DEV_REQUIREMENTS),
        "Dynamic: license-file",
        "",
    ]
    return "\n".join(lines).encode("utf-8")


def _manifest_entry(path: str, content: bytes) -> ManifestEntry:
    """Create the canonical file record used by the minimal M/staging fixture."""
    return ManifestEntry(
        path=path,
        file_type="file",
        mode="0644",
        sha256=_sha256(content),
    )


def _write_wheel_members(wheel: Path, members: dict[str, bytes]) -> None:
    """Write a fixture wheel with a complete RECORD over its final payload."""
    record_name = next(name for name in members if name.endswith(".dist-info/RECORD"))
    rows = []
    for name, content in members.items():
        if name == record_name:
            continue
        digest = urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b"=")
        rows.append(f"{name},sha256={digest.decode('ascii')},{len(content)}")
    members[record_name] = ("\n".join((*rows, f"{record_name},,")) + "\n").encode(
        "utf-8"
    )
    with zipfile.ZipFile(wheel, "w") as archive:
        for name, content in members.items():
            archive.writestr(name, content)


def _write_installable_trial_wheel(
    wheel: Path,
    identity: dict[str, str],
) -> None:
    """Write a minimal valid pure wheel for the two-fresh-venv integration test."""
    dist_info = f"gwexpy_studio-{identity['version']}.dist-info"
    members = {
        "gwexpy_studio/__init__.py": b"",
        "gwexpy_studio/_version.py": (
            f'__version__ = "{identity["version"]}"\n'.encode()
        ),
        "gwexpy_studio/assets/trial-build.json": _canonical_json(
            {**identity, "schema": 1}
        ),
        "gwexpy_studio/assets/io-capabilities.json": _trial_capability_policy_bytes(),
        f"{dist_info}/METADATA": _wheel_metadata(identity["version"]),
        f"{dist_info}/WHEEL": b"Wheel-Version: 1.0\n"
        b"Generator: test\n"
        b"Root-Is-Purelib: true\n"
        b"Tag: py3-none-any\n",
        f"{dist_info}/entry_points.txt": _ENTRY_POINTS,
        f"{dist_info}/top_level.txt": _TOP_LEVEL,
        f"{dist_info}/licenses/LICENSE": _FIXTURE_LICENSE,
        f"{dist_info}/RECORD": b"",
    }
    _write_wheel_members(wheel, members)


def _generated_files(wheel: Path) -> list[dict[str, str]]:
    """Return Task2's reviewed generated delta as recorded by its trial manifest."""
    paths = {
        "src/gwexpy_studio/_version.py": "gwexpy_studio/_version.py",
        "src/gwexpy_studio/assets/trial-build.json": (
            "gwexpy_studio/assets/trial-build.json"
        ),
        "src/gwexpy_studio/assets/io-capabilities.json": (
            "gwexpy_studio/assets/io-capabilities.json"
        ),
    }
    with zipfile.ZipFile(wheel) as archive:
        return [
            {"path": path, "sha256": _sha256(archive.read(member))}
            for path, member in paths.items()
        ]


def _refresh_trial_manifest_wheel_binding(trial_manifest: Path, wheel: Path) -> None:
    """Keep a fixture trial record consistent after regenerating its wheel."""
    document = json.loads(trial_manifest.read_text(encoding="utf-8"))
    document["generated_files"] = _generated_files(wheel)
    document["wheel_sha256"] = _sha256(wheel.read_bytes())
    trial_manifest.write_bytes(_canonical_json(document))


def _rewrite_wheel_member(wheel: Path, member: str, content: bytes) -> None:
    """Replace one fixture member without creating a duplicate ZIP entry."""
    with zipfile.ZipFile(wheel) as archive:
        members = {name: archive.read(name) for name in archive.namelist()}
    members[member] = content
    _write_wheel_members(wheel, members)


def _refresh_staging_manifest(trial_manifest: Path, wheel: Path) -> None:
    """Synchronize the fixture staging sidecar with the generated wheel bytes."""
    members = (
        "gwexpy_studio/_version.py",
        "gwexpy_studio/assets/trial-build.json",
        "gwexpy_studio/assets/io-capabilities.json",
    )
    entries = {
        entry.path: entry
        for entry in read_manifest(
            trial_manifest.with_name("SOURCE-MANIFEST.json")
        ).entries
    }
    with zipfile.ZipFile(wheel) as archive:
        for entry, member in zip(_generated_files(wheel), members, strict=True):
            entries[entry["path"]] = _manifest_entry(
                entry["path"], archive.read(member)
            )
    staging_manifest = trial_manifest.with_name("STAGING-MANIFEST.json")
    staging_manifest.write_bytes(
        ReleaseSourceManifest(
            tuple(
                sorted(entries.values(), key=lambda entry: entry.path.encode("utf-8"))
            )
        ).to_bytes()
    )
    document = json.loads(trial_manifest.read_text(encoding="utf-8"))
    document["staging_manifest_sha256"] = _sha256(staging_manifest.read_bytes())
    trial_manifest.write_bytes(_canonical_json(document))


def _git(checkout: Path, *arguments: str) -> str:
    """Run one deterministic fixture-only Git command."""
    completed = subprocess.run(
        ["git", "-C", str(checkout), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _provenance_checkout(tmp_path: Path) -> tuple[Path, str]:
    """Create a minimal clean public checkout and its canonical source digest."""
    checkout = tmp_path / "public-p"
    package_init = checkout / "src" / "gwexpy_studio" / "__init__.py"
    version = checkout / "src" / "gwexpy_studio" / "_version.py"
    license_file = checkout / "LICENSE"
    policy = checkout / "packaging" / "release-source-allowlist.txt"
    gate = checkout / "scripts" / "run_trial_technical_gate.py"
    version.parent.mkdir(parents=True)
    policy.parent.mkdir(parents=True)
    gate.parent.mkdir(parents=True)
    package_init.write_bytes(b"")
    version.write_text('__version__ = "0.1.0a1"\n', encoding="utf-8")
    license_file.write_bytes(_FIXTURE_LICENSE)
    gate.write_text("# fixture gate script\n", encoding="utf-8")
    policy.write_text(
        "\n".join(
            (
                "include src",
                "include src/**",
                "include LICENSE",
                "include packaging",
                "include packaging/release-source-allowlist.txt",
                "include scripts",
                "include scripts/run_trial_technical_gate.py",
                "exclude .git",
                "exclude .git/**",
                "",
            )
        ),
        encoding="utf-8",
    )
    _git(checkout.parent, "init", checkout.name)
    _git(checkout, "config", "user.email", "trial@example.invalid")
    _git(checkout, "config", "user.name", "Trial Test")
    _git(checkout, "add", ".")
    _git(checkout, "commit", "-m", "public source")
    return checkout, manifest_digest(
        scan_public_checkout(checkout, load_policy(policy))
    )


def _bind_trial_artifact_to_checkout(
    trial_artifact: tuple[Path, Path, Path, dict[str, str]], tmp_path: Path
) -> tuple[Path, Path, Path, dict[str, str], Path]:
    """Bind a minimal installable wheel to a real clean P for capture tests."""
    _old_wheel, trial_manifest, source_manifest, _old_identity = trial_artifact
    checkout, _unused_manifest_sha256 = _provenance_checkout(tmp_path)
    source = scan_public_checkout(
        checkout, load_policy(checkout / "packaging" / "release-source-allowlist.txt")
    )
    source_manifest.write_bytes(source.to_bytes())
    source_sha = _git(checkout, "rev-parse", "HEAD")
    version = f"0.1.0a1+trial.p.g{source_sha[:7]}.20260907.r1.a1"
    identity = {
        "build_id": f"P-{source_sha[:7]}-20260907-r1-a1",
        "source_manifest_sha256": manifest_digest(source),
        "source_sha": source_sha,
        "version": version,
    }
    wheel = source_manifest.with_name(f"gwexpy_studio-{version}-py3-none-any.whl")
    _write_installable_trial_wheel(wheel, identity)
    generated = _generated_files(wheel)
    generated_members = (
        "gwexpy_studio/_version.py",
        "gwexpy_studio/assets/trial-build.json",
        "gwexpy_studio/assets/io-capabilities.json",
    )
    entries = {entry.path: entry for entry in source.entries}
    with zipfile.ZipFile(wheel) as archive:
        for generated_entry, member in zip(generated, generated_members, strict=True):
            current = entries.get(generated_entry["path"])
            entries[generated_entry["path"]] = ManifestEntry(
                path=generated_entry["path"],
                file_type="file",
                mode="0644" if current is None else current.mode,
                sha256=_sha256(archive.read(member)),
            )
    staging = ReleaseSourceManifest(
        tuple(sorted(entries.values(), key=lambda entry: entry.path.encode("utf-8")))
    )
    staging_path = trial_manifest.with_name("STAGING-MANIFEST.json")
    staging_path.write_bytes(staging.to_bytes())
    trial_manifest.write_bytes(
        _canonical_json(
            {
                **identity,
                "generated_files": generated,
                "schema": 1,
                "staging_manifest_sha256": manifest_digest(staging),
                "wheel_filename": wheel.name,
                "wheel_sha256": _sha256(wheel.read_bytes()),
            }
        )
    )
    return wheel, trial_manifest, source_manifest, identity, checkout


def test_capture_executes_the_sealed_git_p_gate_after_path_replacement(
    tmp_path: Path,
) -> None:
    """A replacement after sealing cannot change the bytes executed by Python."""
    checkout = tmp_path / "public-p"
    script = checkout / "scripts" / "run_trial_technical_gate.py"
    script.parent.mkdir(parents=True)
    trusted = (
        b"from pathlib import Path\n"
        b"import sys\n"
        b"def argument(name):\n"
        b"    return sys.argv[sys.argv.index(name) + 1]\n"
        b"phase = argument('--phase') if '--phase' in sys.argv else 'master'\n"
        b"target = (\n"
        b"    Path(argument('--result'))\n"
        b"    if phase == 'master'\n"
        b"    else Path(argument('--work-root')) / f'{phase}.txt'\n"
        b")\n"
        b"target.write_text(f'trusted:{phase}', encoding='utf-8')\n"
    )
    script.write_bytes(trusted)
    _git(checkout.parent, "init", checkout.name)
    _git(checkout, "config", "user.email", "trial@example.invalid")
    _git(checkout, "config", "user.name", "Trial Test")
    _git(checkout, "add", "scripts/run_trial_technical_gate.py")
    _git(checkout, "commit", "-m", "trusted gate")
    source_sha = _git(checkout, "rev-parse", "HEAD")
    script.write_bytes(b"raise RuntimeError('mutable checkout code ran')\n")
    workspace = tmp_path / "private-work"
    workspace.mkdir()
    result = workspace / "technical-gate.json"

    with _resolution().seal_technical_gate_script(
        checkout=checkout, source_sha=source_sha, workspace=workspace
    ) as gate_fd:
        script.write_bytes(b"raise RuntimeError('post-seal replacement ran')\n")
        master = _gate().technical_gate_command(
            python=Path(sys.executable),
            gate_fd=gate_fd,
            checkout_root=checkout,
            work_root=workspace,
            result_path=result,
        )
        master_completed = subprocess.run(
            master,
            capture_output=True,
            check=False,
            pass_fds=(gate_fd,),
            text=True,
        )
        script.write_bytes(b"raise RuntimeError('post-master replacement ran')\n")
        producer = _gate()._phase_command(
            phase="producer",
            python=Path(sys.executable),
            gate_fd=gate_fd,
            checkout=checkout,
            work_root=workspace,
        )
        producer_completed = subprocess.run(
            producer,
            capture_output=True,
            check=False,
            pass_fds=(gate_fd,),
            text=True,
        )
        (workspace / "trial.gwxproj").write_text("saved", encoding="utf-8")
        script.write_bytes(b"raise RuntimeError('post-producer replacement ran')\n")
        consumer = _gate()._phase_command(
            phase="consumer",
            python=Path(sys.executable),
            gate_fd=gate_fd,
            checkout=checkout,
            work_root=workspace,
            project=workspace / "trial.gwxproj",
        )
        consumer_completed = subprocess.run(
            consumer,
            capture_output=True,
            check=False,
            pass_fds=(gate_fd,),
            text=True,
        )

    assert master_completed.returncode == 0, master_completed.stderr
    assert producer_completed.returncode == 0, producer_completed.stderr
    assert consumer_completed.returncode == 0, consumer_completed.stderr
    assert result.read_text(encoding="utf-8") == "trusted:master"
    assert (workspace / "producer.txt").read_text(encoding="utf-8") == (
        "trusted:producer"
    )
    assert (workspace / "consumer.txt").read_text(encoding="utf-8") == (
        "trusted:consumer"
    )
    assert not (workspace / _resolution()._SEALED_GATE_SCRIPT_NAME).exists()
    with pytest.raises(OSError):
        os.fstat(gate_fd)


@pytest.fixture
def trial_artifact(tmp_path: Path) -> tuple[Path, Path, Path, dict[str, str]]:
    """Create a small, internally consistent trial-wheel evidence set."""
    source_manifest = tmp_path / "SOURCE-MANIFEST.json"
    source_version = b'__version__ = "0.1.0a1"\n'
    source_entries = (
        _manifest_entry("LICENSE", _FIXTURE_LICENSE),
        _manifest_entry("src/gwexpy_studio/__init__.py", b""),
        _manifest_entry("src/gwexpy_studio/_version.py", source_version),
    )
    source_manifest.write_bytes(
        ReleaseSourceManifest(
            entries=tuple(sorted(source_entries, key=lambda entry: entry.path.encode()))
        ).to_bytes()
    )
    staging_manifest = tmp_path / "STAGING-MANIFEST.json"
    source_manifest_sha256 = _sha256(source_manifest.read_bytes())
    identity = {
        "build_id": "P-abcdef0-20260907-r1-a1",
        "source_manifest_sha256": source_manifest_sha256,
        "source_sha": "abcdef0123456789abcdef0123456789abcdef01",
        "version": "0.1.0a1+trial.p.gabcdef0.20260907.r1.a1",
    }
    wheel = tmp_path / (
        "gwexpy_studio-0.1.0a1+trial.p.gabcdef0.20260907.r1.a1-py3-none-any.whl"
    )
    _write_installable_trial_wheel(wheel, identity)
    entries = {entry.path: entry for entry in read_manifest(source_manifest).entries}
    with zipfile.ZipFile(wheel) as archive:
        for entry, member in zip(
            _generated_files(wheel),
            (
                "gwexpy_studio/_version.py",
                "gwexpy_studio/assets/trial-build.json",
                "gwexpy_studio/assets/io-capabilities.json",
            ),
            strict=True,
        ):
            entries[entry["path"]] = _manifest_entry(
                entry["path"], archive.read(member)
            )
    staging_manifest.write_bytes(
        ReleaseSourceManifest(
            tuple(
                sorted(entries.values(), key=lambda entry: entry.path.encode("utf-8"))
            )
        ).to_bytes()
    )
    trial_manifest = tmp_path / "TRIAL-MANIFEST.json"
    trial_manifest.write_bytes(
        _canonical_json(
            {
                **identity,
                "generated_files": _generated_files(wheel),
                "schema": 1,
                "staging_manifest_sha256": _sha256(staging_manifest.read_bytes()),
                "wheel_filename": wheel.name,
                "wheel_sha256": _sha256(wheel.read_bytes()),
            }
        )
    )
    return wheel, trial_manifest, source_manifest, identity


def test_trial_artifact_binds_wheel_and_both_manifests(
    trial_artifact: tuple[Path, Path, Path, dict[str, str]],
) -> None:
    """Resolution begins only from a wheel bound to M and its trial record."""
    wheel, trial_manifest, source_manifest, identity = trial_artifact

    artifact = _resolution().load_trial_artifact(
        wheel=wheel,
        trial_manifest=trial_manifest,
        source_manifest=source_manifest,
    )

    assert artifact.build_id == identity["build_id"]
    assert artifact.source_sha == identity["source_sha"]
    assert artifact.source_manifest_sha256 == identity["source_manifest_sha256"]
    assert artifact.staging_manifest_sha256 == _sha256(
        trial_manifest.with_name("STAGING-MANIFEST.json").read_bytes()
    )
    assert artifact.trial_manifest_sha256 == _sha256(trial_manifest.read_bytes())
    assert artifact.wheel_filename == wheel.name
    assert artifact.wheel_sha256 == _sha256(wheel.read_bytes())


def test_resolution_rejects_an_added_metadata_dependency_before_pip(
    trial_artifact: tuple[Path, Path, Path, dict[str, str]],
) -> None:
    """Resolver capture never creates a venv for an unapproved dependency set."""
    wheel, trial_manifest, source_manifest, _identity = trial_artifact
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
    _refresh_trial_manifest_wheel_binding(trial_manifest, wheel)

    with pytest.raises(_resolution().ResolutionError, match="metadata"):
        _resolution().load_trial_artifact(
            wheel=wheel,
            trial_manifest=trial_manifest,
            source_manifest=source_manifest,
        )


def test_capture_rejects_a_checkout_with_a_different_git_head(
    trial_artifact: tuple[Path, Path, Path, dict[str, str]], tmp_path: Path
) -> None:
    """Capture never accepts a wheel when its supplied checkout is no longer P."""
    wheel, trial_manifest, source_manifest, _identity = trial_artifact
    artifact = _resolution().load_trial_artifact(
        wheel=wheel,
        trial_manifest=trial_manifest,
        source_manifest=source_manifest,
    )
    checkout, manifest_sha256 = _provenance_checkout(tmp_path)
    bound = replace(
        artifact,
        source_sha=_git(checkout, "rev-parse", "HEAD"),
        source_manifest_sha256=manifest_sha256,
    )
    (checkout / "README.md").write_text("new head\n", encoding="utf-8")
    _git(checkout, "add", "README.md")
    _git(checkout, "commit", "-m", "advance public source")

    with pytest.raises(_resolution().ResolutionError, match="checkout identity"):
        _resolution().verify_capture_checkout(checkout, bound)


def test_capture_accepts_the_exact_clean_checkout_and_manifest(
    trial_artifact: tuple[Path, Path, Path, dict[str, str]], tmp_path: Path
) -> None:
    """The checkout proves P only when its clean HEAD and canonical M agree."""
    wheel, trial_manifest, source_manifest, _identity = trial_artifact
    artifact = _resolution().load_trial_artifact(
        wheel=wheel,
        trial_manifest=trial_manifest,
        source_manifest=source_manifest,
    )
    checkout, manifest_sha256 = _provenance_checkout(tmp_path)
    bound = replace(
        artifact,
        source_sha=_git(checkout, "rev-parse", "HEAD"),
        source_manifest_sha256=manifest_sha256,
    )

    _resolution().verify_capture_checkout(checkout, bound)


def test_capture_rejects_a_dirty_checkout(
    trial_artifact: tuple[Path, Path, Path, dict[str, str]], tmp_path: Path
) -> None:
    """Capture treats an uncommitted public source change as an identity failure."""
    wheel, trial_manifest, source_manifest, _identity = trial_artifact
    artifact = _resolution().load_trial_artifact(
        wheel=wheel,
        trial_manifest=trial_manifest,
        source_manifest=source_manifest,
    )
    checkout, manifest_sha256 = _provenance_checkout(tmp_path)
    bound = replace(
        artifact,
        source_sha=_git(checkout, "rev-parse", "HEAD"),
        source_manifest_sha256=manifest_sha256,
    )
    (checkout / "untracked.txt").write_text("dirty\n", encoding="utf-8")

    with pytest.raises(_resolution().ResolutionError, match="checkout identity"):
        _resolution().verify_capture_checkout(checkout, bound)


def test_capture_rejects_a_checkout_whose_manifest_is_not_m(
    trial_artifact: tuple[Path, Path, Path, dict[str, str]], tmp_path: Path
) -> None:
    """The checkout must reproduce the artifact's canonical source manifest."""
    wheel, trial_manifest, source_manifest, _identity = trial_artifact
    artifact = _resolution().load_trial_artifact(
        wheel=wheel,
        trial_manifest=trial_manifest,
        source_manifest=source_manifest,
    )
    checkout, manifest_sha256 = _provenance_checkout(tmp_path)
    bound = replace(
        artifact,
        source_sha=_git(checkout, "rev-parse", "HEAD"),
        source_manifest_sha256=manifest_sha256,
    )

    with pytest.raises(_resolution().ResolutionError, match="source manifest"):
        _resolution().verify_capture_checkout(
            checkout, replace(bound, source_manifest_sha256="0" * 64)
        )


def test_resolution_projection_keeps_only_artifact_identity_not_urls_or_paths(
    trial_artifact: tuple[Path, Path, Path, dict[str, str]],
    tmp_path: Path,
) -> None:
    """Persisted evidence cannot expose source paths, hosts, or credentials."""
    wheel, trial_manifest, source_manifest, _identity = trial_artifact
    artifact = _resolution().load_trial_artifact(
        wheel=wheel,
        trial_manifest=trial_manifest,
        source_manifest=source_manifest,
    )
    report = {
        "environment": {"implementation_name": "cpython"},
        "install": [
            {
                "download_info": {
                    "archive_info": {"hashes": {"sha256": artifact.wheel_sha256}},
                    "url": f"file://{wheel}",
                },
                "is_direct": True,
                "is_yanked": False,
                "metadata": {
                    "name": "gwexpy-studio",
                    "version": artifact.version,
                },
            },
            {
                "download_info": {
                    "archive_info": {"hashes": {"sha256": "c" * 64}},
                    "url": (
                        "https://trial-user:secret@example.invalid/wheels/"
                        "numpy-2.3.1-cp312-cp312-manylinux_2_28_x86_64.whl"
                        "?token=private"
                    ),
                },
                "is_direct": False,
                "is_yanked": False,
                "metadata": {"name": "NumPy", "version": "2.3.1"},
            },
        ],
        "pip_version": "25.3",
        "version": "1",
    }
    inspect = {
        "environment": {"platform_machine": "x86_64", "sys_prefix": str(tmp_path)},
        "installed": [
            {
                "metadata": {"name": "gwexpy-studio", "version": artifact.version},
                "metadata_location": str(tmp_path / "site-packages"),
            },
            {
                "metadata": {"name": "numpy", "version": "2.3.1"},
                "metadata_location": "/private/host/site-packages/numpy.dist-info",
            },
        ],
        "pip_version": "25.3",
        "version": "1",
    }

    projection = _resolution().build_resolution_projection(
        artifact=artifact,
        machine="x86_64",
        glibc_version="2.39",
        os_runtime=_os_runtime(),
        python_version="3.12.12",
        pip_version="25.3",
        phase_one_report=report,
        phase_one_inspect=inspect,
        phase_two_report=report,
        phase_two_inspect=inspect,
    )
    encoded = _canonical_json(projection).decode("utf-8")

    assert projection["architecture"] == "x86_64"
    assert projection["schema"] == 2
    assert projection["os_runtime"] == _os_runtime()
    assert projection["runtime_artifacts"] == [
        {
            "filename": "numpy-2.3.1-cp312-cp312-manylinux_2_28_x86_64.whl",
            "name": "numpy",
            "sha256": "c" * 64,
            "version": "2.3.1",
        }
    ]
    assert "trial-user" not in encoded
    assert "secret" not in encoded
    assert "example.invalid" not in encoded
    assert str(tmp_path) not in encoded
    assert "/private/host" not in encoded
    assert "sys_prefix" not in encoded
    assert projection["phase_one"]["pip_report_sha256"] == _sha256(
        _canonical_json(projection["phase_one"]["report"])
    )
    assert projection["phase_one"]["pip_inspect_sha256"] == _sha256(
        _canonical_json(projection["phase_one"]["inspect"])
    )


@pytest.mark.parametrize(
    ("machine", "dpkg_architecture"),
    [("x86_64", "amd64"), ("aarch64", "arm64")],
)
def test_resolution_captures_only_direct_qt_gl_runtime_packages(
    machine: str,
    dpkg_architecture: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A native qualification records the two direct Qt GL runtime packages."""
    module = _resolution()
    seen: dict[str, object] = {}

    def query(command: object, **kwargs: object) -> SimpleNamespace:
        seen["command"] = command
        seen["kwargs"] = kwargs
        return SimpleNamespace(
            returncode=0,
            stdout=(
                f"ii \tlibegl1\t1.7.0-1build1\t{dpkg_architecture}\n"
                f"ii \tlibgl1\t1.7.0-1build1\t{dpkg_architecture}\n"
            ),
        )

    loaded: list[str] = []

    def load(library: str) -> object:
        loaded.append(library)
        return object()

    monkeypatch.setattr(module.subprocess, "run", query)
    monkeypatch.setattr(module, "ctypes", SimpleNamespace(CDLL=load), raising=False)

    assert module.capture_os_runtime(machine, cwd=tmp_path) == _os_runtime(machine)
    assert seen["command"] == (
        "dpkg-query",
        "--show",
        "--showformat=${db:Status-Abbrev}\\t${Package}\\t${Version}\\t${Architecture}\\n",
        "libegl1",
        "libgl1",
    )
    assert loaded == ["libEGL.so.1", "libGL.so.1"]


@pytest.mark.parametrize(
    ("stdout", "machine", "load_error"),
    [
        (
            "hi \tlibegl1\t1.7.0-1build1\tamd64\nii \tlibgl1\t1.7.0-1build1\tamd64\n",
            "x86_64",
            None,
        ),
        (
            "ii libegl1 1.7.0-1build1 amd64\nii \tlibgl1\t1.7.0-1build1\tamd64\n",
            "x86_64",
            None,
        ),
        (
            "ii \tlibegl1\t1.7.0-1build1\tarm64\nii \tlibgl1\t1.7.0-1build1\tarm64\n",
            "x86_64",
            None,
        ),
        (
            "ii \tlibegl1\t/private/path\tamd64\nii \tlibgl1\t1.7.0-1build1\tamd64\n",
            "x86_64",
            None,
        ),
        (
            "ii \tlibegl1\t1.7.0-1build1\tamd64\nii \tlibgl1\t1.7.0-1build1\tamd64\n",
            "x86_64",
            "libGL.so.1",
        ),
    ],
)
def test_resolution_rejects_invalid_direct_qt_gl_runtime(
    stdout: str,
    machine: str,
    load_error: str | None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing, unsafe, wrong-architecture, or unloadable GL evidence fails closed."""
    module = _resolution()
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout=stdout),
    )

    def load(library: str) -> object:
        if library == load_error:
            raise OSError("missing")
        return object()

    monkeypatch.setattr(module, "ctypes", SimpleNamespace(CDLL=load), raising=False)

    with pytest.raises(module.ResolutionError, match="Qt GL runtime"):
        module.capture_os_runtime(machine, cwd=tmp_path)


def test_resolution_rejects_an_unqueryable_direct_qt_gl_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed dpkg query cannot be treated as installed runtime evidence."""
    module = _resolution()
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=1, stdout=""),
    )

    with pytest.raises(module.ResolutionError, match="package query failed"):
        module.capture_os_runtime("x86_64", cwd=tmp_path)


def test_resolution_requires_explicit_installed_status_in_qt_gl_runtime() -> None:
    """A static resolution record must attest that each package is installed."""
    module = _resolution()
    runtime = {
        "manager": "dpkg",
        "packages": [
            {
                "architecture": "amd64",
                "name": "libegl1",
                "version": "1.7.0-1build1",
            },
            {
                "architecture": "amd64",
                "name": "libgl1",
                "version": "1.7.0-1build1",
            },
        ],
        "required_libraries": ["libEGL.so.1", "libGL.so.1"],
    }

    with pytest.raises(module.ResolutionError, match="Qt GL runtime package"):
        module.validate_os_runtime(runtime, machine="x86_64")


def test_resolution_requires_ubuntu_2404_os_release(tmp_path: Path) -> None:
    """The architecture evidence is valid only for the declared release target."""
    release = tmp_path / "os-release"
    release.write_text('ID=ubuntu\nVERSION_ID="24.04"\n', encoding="utf-8")

    assert _resolution()._ubuntu_release(release) == ("ubuntu", "24.04")

    release.write_text('ID=debian\nVERSION_ID="12"\n', encoding="utf-8")
    with pytest.raises(_resolution().ResolutionError, match="Ubuntu 24.04"):
        _resolution()._ubuntu_release(release)


def test_resolution_rejects_gate_identity_that_disagrees_with_the_wheel(
    trial_artifact: tuple[Path, Path, Path, dict[str, str]],
) -> None:
    """The passed UI workflow must attest to the same installed trial wheel."""
    wheel, trial_manifest, source_manifest, _identity = trial_artifact
    artifact = _resolution().load_trial_artifact(
        wheel=wheel,
        trial_manifest=trial_manifest,
        source_manifest=source_manifest,
    )
    report = {
        "install": [
            {
                "download_info": {
                    "archive_info": {"hashes": {"sha256": artifact.wheel_sha256}},
                    "url": f"file://{wheel}",
                },
                "is_direct": True,
                "is_yanked": False,
                "metadata": {"name": "gwexpy-studio", "version": artifact.version},
            }
        ],
        "pip_version": "25.3",
        "version": "1",
    }
    inspect = {
        "installed": [
            {"metadata": {"name": "gwexpy-studio", "version": artifact.version}}
        ],
        "pip_version": "25.3",
        "version": "1",
    }
    gate = {
        "architecture": "x86_64",
        "checks": {name: True for name in _gate()._CHECK_NAMES},
        "installed": {
            "build_id": artifact.build_id,
            "source_sha": "0" * 40,
            "version": artifact.version,
        },
        "python_version": "3.12.12",
        "schema": 2,
        "status": "passed",
    }

    with pytest.raises(_resolution().ResolutionError, match="identity disagrees"):
        _resolution().build_resolution_projection(
            artifact=artifact,
            machine="x86_64",
            glibc_version="2.39",
            os_runtime=_os_runtime(),
            python_version="3.12.12",
            pip_version="25.3",
            phase_one_report=report,
            phase_one_inspect=inspect,
            phase_two_report=report,
            phase_two_inspect=inspect,
            technical_gate=gate,
        )


def test_constraints_are_sorted_exact_runtime_pins_and_strict_pip_flags_are_used(
    trial_artifact: tuple[Path, Path, Path, dict[str, str]],
) -> None:
    """Both installation phases use fresh binary-only, noninteractive pip."""
    wheel, trial_manifest, source_manifest, _identity = trial_artifact
    artifact = _resolution().load_trial_artifact(
        wheel=wheel,
        trial_manifest=trial_manifest,
        source_manifest=source_manifest,
    )
    report = {
        "install": [
            {
                "download_info": {
                    "archive_info": {"hashes": {"sha256": artifact.wheel_sha256}},
                    "url": f"file://{wheel}",
                },
                "is_direct": True,
                "is_yanked": False,
                "metadata": {
                    "name": "gwexpy-studio",
                    "version": artifact.version,
                },
            },
            {
                "download_info": {
                    "archive_info": {"hashes": {"sha256": "d" * 64}},
                    "url": "https://files.pythonhosted.org/packages/zeta-1.0.0.whl",
                },
                "is_direct": False,
                "is_yanked": False,
                "metadata": {"name": "Zeta", "version": "1.0.0"},
            },
            {
                "download_info": {
                    "archive_info": {"hashes": {"sha256": "e" * 64}},
                    "url": "https://files.pythonhosted.org/packages/alpha-2.0.0.whl",
                },
                "is_direct": False,
                "is_yanked": False,
                "metadata": {"name": "alpha", "version": "2.0.0"},
            },
        ],
        "version": "1",
    }

    constraints = _resolution().runtime_constraints(report, artifact=artifact)
    command = _resolution().pip_install_command(
        python="/fresh/venv/bin/python",
        wheel=wheel,
        report_path=Path("/private/report.json"),
        constraints_path=Path("/private/constraints.txt"),
    )

    assert constraints == "alpha==2.0.0\nzeta==1.0.0\n"
    assert "--isolated" in command
    assert "--no-input" in command
    assert "--only-binary=:all:" in command
    assert command.count("--isolated") == 1
    assert command.index("--only-binary=:all:") < command.index(str(wheel))


def test_resolution_accepts_pip_hashes_sha256_without_legacy_hash(
    trial_artifact: tuple[Path, Path, Path, dict[str, str]],
) -> None:
    """The stable pip report hash field is sufficient evidence for a wheel."""
    wheel, trial_manifest, source_manifest, _identity = trial_artifact
    artifact = _resolution().load_trial_artifact(
        wheel=wheel,
        trial_manifest=trial_manifest,
        source_manifest=source_manifest,
    )
    report = {
        "install": [
            {
                "download_info": {
                    "archive_info": {"hashes": {"sha256": artifact.wheel_sha256}},
                    "url": f"file://{wheel}",
                },
                "is_direct": True,
                "is_yanked": False,
                "metadata": {
                    "name": "gwexpy-studio",
                    "version": artifact.version,
                },
            }
        ],
        "pip_version": "25.3",
        "version": "1",
    }

    assert _resolution().runtime_constraints(report, artifact=artifact) == ""


def test_resolution_rejects_conflicting_legacy_and_stable_pip_hashes(
    trial_artifact: tuple[Path, Path, Path, dict[str, str]],
) -> None:
    """A legacy digest may accompany, but never contradict, hashes.sha256."""
    wheel, trial_manifest, source_manifest, _identity = trial_artifact
    artifact = _resolution().load_trial_artifact(
        wheel=wheel,
        trial_manifest=trial_manifest,
        source_manifest=source_manifest,
    )
    report = {
        "install": [
            {
                "download_info": {
                    "archive_info": {
                        "hash": f"sha256={artifact.wheel_sha256}",
                        "hashes": {"sha256": "f" * 64},
                    },
                    "url": f"file://{wheel}",
                },
                "is_direct": True,
                "is_yanked": False,
                "metadata": {
                    "name": "gwexpy-studio",
                    "version": artifact.version,
                },
            }
        ],
        "pip_version": "25.3",
        "version": "1",
    }

    with pytest.raises(_resolution().ResolutionError, match="hash"):
        _resolution().runtime_constraints(report, artifact=artifact)


def test_resolution_rejects_an_unsupported_pip_report_schema(
    trial_artifact: tuple[Path, Path, Path, dict[str, str]],
) -> None:
    """The resolver refuses a report whose documented schema changed."""
    wheel, trial_manifest, source_manifest, _identity = trial_artifact
    artifact = _resolution().load_trial_artifact(
        wheel=wheel,
        trial_manifest=trial_manifest,
        source_manifest=source_manifest,
    )
    report = {
        "install": [
            {
                "download_info": {
                    "archive_info": {"hashes": {"sha256": artifact.wheel_sha256}},
                    "url": f"file://{wheel}",
                },
                "is_direct": True,
                "is_yanked": False,
                "metadata": {
                    "name": "gwexpy-studio",
                    "version": artifact.version,
                },
            }
        ],
        "pip_version": "25.3",
        "version": "2",
    }

    with pytest.raises(_resolution().ResolutionError, match="schema"):
        _resolution().runtime_constraints(report, artifact=artifact)


def test_resolution_rejects_an_unsupported_pip_inspect_schema(
    trial_artifact: tuple[Path, Path, Path, dict[str, str]],
) -> None:
    """The resolver refuses an inspect record whose documented schema changed."""
    wheel, trial_manifest, source_manifest, _identity = trial_artifact
    artifact = _resolution().load_trial_artifact(
        wheel=wheel,
        trial_manifest=trial_manifest,
        source_manifest=source_manifest,
    )
    report = {
        "install": [
            {
                "download_info": {
                    "archive_info": {"hashes": {"sha256": artifact.wheel_sha256}},
                    "url": f"file://{wheel}",
                },
                "is_direct": True,
                "is_yanked": False,
                "metadata": {
                    "name": "gwexpy-studio",
                    "version": artifact.version,
                },
            }
        ],
        "pip_version": "25.3",
        "version": "1",
    }
    inspect = {
        "installed": [
            {
                "metadata": {
                    "name": "gwexpy-studio",
                    "version": artifact.version,
                }
            }
        ],
        "pip_version": "25.3",
        "version": "2",
    }

    with pytest.raises(_resolution().ResolutionError, match="schema"):
        _resolution().build_resolution_projection(
            artifact=artifact,
            machine="x86_64",
            glibc_version="2.39",
            os_runtime=_os_runtime(),
            python_version="3.12.12",
            pip_version="25.3",
            phase_one_report=report,
            phase_one_inspect=inspect,
            phase_two_report=report,
            phase_two_inspect=inspect,
        )


def test_resolution_rejects_a_package_version_that_would_leak_a_path(
    trial_artifact: tuple[Path, Path, Path, dict[str, str]],
) -> None:
    """Persisted pins must not accept a path-like package-version field."""
    wheel, trial_manifest, source_manifest, _identity = trial_artifact
    artifact = _resolution().load_trial_artifact(
        wheel=wheel,
        trial_manifest=trial_manifest,
        source_manifest=source_manifest,
    )
    report = {
        "install": [
            {
                "download_info": {
                    "archive_info": {"hashes": {"sha256": artifact.wheel_sha256}},
                    "url": f"file://{wheel}",
                },
                "is_direct": True,
                "is_yanked": False,
                "metadata": {
                    "name": "gwexpy-studio",
                    "version": artifact.version,
                },
            },
            {
                "download_info": {
                    "archive_info": {"hashes": {"sha256": "a" * 64}},
                    "url": "https://files.pythonhosted.org/packages/numpy.whl",
                },
                "is_direct": False,
                "is_yanked": False,
                "metadata": {"name": "numpy", "version": "2.3+/private/host"},
            },
        ],
        "pip_version": "25.3",
        "version": "1",
    }

    with pytest.raises(_resolution().ResolutionError, match="package version"):
        _resolution().runtime_constraints(report, artifact=artifact)


def test_resolution_rejects_a_pip_version_that_would_leak_a_path(
    trial_artifact: tuple[Path, Path, Path, dict[str, str]],
) -> None:
    """The recorded pip identity cannot carry a path-like version string."""
    wheel, trial_manifest, source_manifest, _identity = trial_artifact
    artifact = _resolution().load_trial_artifact(
        wheel=wheel,
        trial_manifest=trial_manifest,
        source_manifest=source_manifest,
    )
    report = {
        "install": [
            {
                "download_info": {
                    "archive_info": {"hashes": {"sha256": artifact.wheel_sha256}},
                    "url": f"file://{wheel}",
                },
                "is_direct": True,
                "is_yanked": False,
                "metadata": {
                    "name": "gwexpy-studio",
                    "version": artifact.version,
                },
            }
        ],
        "pip_version": "25.3+/private/host",
        "version": "1",
    }
    inspect = {
        "installed": [
            {
                "metadata": {
                    "name": "gwexpy-studio",
                    "version": artifact.version,
                }
            }
        ],
        "pip_version": "25.3+/private/host",
        "version": "1",
    }

    with pytest.raises(_resolution().ResolutionError, match="pip version"):
        _resolution().build_resolution_projection(
            artifact=artifact,
            machine="x86_64",
            glibc_version="2.39",
            os_runtime=_os_runtime(),
            python_version="3.12.12",
            pip_version="25.3+/private/host",
            phase_one_report=report,
            phase_one_inspect=inspect,
            phase_two_report=report,
            phase_two_inspect=inspect,
        )


def test_resolution_rejects_a_non_studio_direct_dependency(
    trial_artifact: tuple[Path, Path, Path, dict[str, str]],
) -> None:
    """A public name==version lock cannot preserve a second direct artifact URL."""
    wheel, trial_manifest, source_manifest, _identity = trial_artifact
    artifact = _resolution().load_trial_artifact(
        wheel=wheel,
        trial_manifest=trial_manifest,
        source_manifest=source_manifest,
    )
    report = {
        "install": [
            {
                "download_info": {
                    "archive_info": {"hashes": {"sha256": artifact.wheel_sha256}},
                    "url": f"file://{wheel}",
                },
                "is_direct": True,
                "is_yanked": False,
                "metadata": {
                    "name": "gwexpy-studio",
                    "version": artifact.version,
                },
            },
            {
                "download_info": {
                    "archive_info": {"hashes": {"sha256": "a" * 64}},
                    "url": "https://private.example.invalid/numpy-2.3.1.whl",
                },
                "is_direct": True,
                "is_yanked": False,
                "metadata": {"name": "numpy", "version": "2.3.1"},
            },
        ],
        "pip_version": "25.3",
        "version": "1",
    }

    with pytest.raises(_resolution().ResolutionError, match="direct"):
        _resolution().runtime_constraints(report, artifact=artifact)


def test_resolution_rejects_a_yanked_runtime_artifact(
    trial_artifact: tuple[Path, Path, Path, dict[str, str]],
) -> None:
    """A yanked binary cannot be locked for a repeatable participant install."""
    wheel, trial_manifest, source_manifest, _identity = trial_artifact
    artifact = _resolution().load_trial_artifact(
        wheel=wheel,
        trial_manifest=trial_manifest,
        source_manifest=source_manifest,
    )
    report = {
        "install": [
            {
                "download_info": {
                    "archive_info": {"hashes": {"sha256": artifact.wheel_sha256}},
                    "url": f"file://{wheel}",
                },
                "is_direct": True,
                "is_yanked": False,
                "metadata": {
                    "name": "gwexpy-studio",
                    "version": artifact.version,
                },
            },
            {
                "download_info": {
                    "archive_info": {"hashes": {"sha256": "a" * 64}},
                    "url": "https://files.pythonhosted.org/packages/numpy-2.3.1.whl",
                },
                "is_direct": False,
                "is_yanked": True,
                "metadata": {"name": "numpy", "version": "2.3.1"},
            },
        ],
        "pip_version": "25.3",
        "version": "1",
    }

    with pytest.raises(_resolution().ResolutionError, match="yanked"):
        _resolution().runtime_constraints(report, artifact=artifact)


def test_resolution_subprocess_environment_cannot_inherit_another_python_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fresh venv commands ignore ambient pip, Python, and conda overlays."""
    monkeypatch.setenv("CONDA_PREFIX", "/private/conda")
    monkeypatch.setenv("PIP_INDEX_URL", "https://user:secret@example.invalid/simple")
    monkeypatch.setenv("PYTHONHOME", "/private/python")
    monkeypatch.setenv("PYTHONPATH", "/private/checkout/src")
    monkeypatch.setenv("VIRTUAL_ENV", "/private/venv")

    environment = _resolution()._subprocess_environment()

    for name in (
        "CONDA_PREFIX",
        "PIP_INDEX_URL",
        "PYTHONHOME",
        "PYTHONPATH",
        "VIRTUAL_ENV",
    ):
        assert name not in environment


def test_resolution_rejects_a_trial_manifest_with_the_wrong_source_digest(
    trial_artifact: tuple[Path, Path, Path, dict[str, str]],
) -> None:
    """The detached M sidecar cannot be substituted after wheel construction."""
    wheel, trial_manifest, source_manifest, _identity = trial_artifact
    document = json.loads(trial_manifest.read_text(encoding="utf-8"))
    document["source_manifest_sha256"] = "f" * 64
    trial_manifest.write_bytes(_canonical_json(document))

    with pytest.raises(_resolution().ResolutionError, match="source manifest"):
        _resolution().load_trial_artifact(
            wheel=wheel,
            trial_manifest=trial_manifest,
            source_manifest=source_manifest,
        )


def test_resolution_rejects_a_trial_manifest_with_the_wrong_staging_digest(
    trial_artifact: tuple[Path, Path, Path, dict[str, str]],
) -> None:
    """The reviewed staging delta sidecar cannot be substituted after build."""
    wheel, trial_manifest, source_manifest, _identity = trial_artifact
    document = json.loads(trial_manifest.read_text(encoding="utf-8"))
    document["staging_manifest_sha256"] = "f" * 64
    trial_manifest.write_bytes(_canonical_json(document))

    with pytest.raises(_resolution().ResolutionError, match="staging manifest"):
        _resolution().load_trial_artifact(
            wheel=wheel,
            trial_manifest=trial_manifest,
            source_manifest=source_manifest,
        )


def test_resolution_rejects_a_staging_manifest_outside_the_reviewed_delta(
    trial_artifact: tuple[Path, Path, Path, dict[str, str]],
) -> None:
    """Matching a digest is insufficient when staging differs from M illegally."""
    wheel, trial_manifest, source_manifest, _identity = trial_artifact
    staging_manifest = trial_manifest.with_name("STAGING-MANIFEST.json")
    staging_manifest.write_bytes(
        ReleaseSourceManifest(
            entries=(_manifest_entry("unexpected.txt", b"unexpected"),)
        ).to_bytes()
    )
    document = json.loads(trial_manifest.read_text(encoding="utf-8"))
    document["staging_manifest_sha256"] = _sha256(staging_manifest.read_bytes())
    trial_manifest.write_bytes(_canonical_json(document))

    with pytest.raises(_resolution().ResolutionError, match="staging delta"):
        _resolution().load_trial_artifact(
            wheel=wheel,
            trial_manifest=trial_manifest,
            source_manifest=source_manifest,
        )


def test_resolution_rejects_a_generated_wheel_member_not_matching_trial_manifest(
    trial_artifact: tuple[Path, Path, Path, dict[str, str]],
) -> None:
    """TRIAL's approved staging delta is checked against the installed wheel bytes."""
    wheel, trial_manifest, source_manifest, _identity = trial_artifact
    document = json.loads(trial_manifest.read_text(encoding="utf-8"))
    document["generated_files"][1]["sha256"] = "f" * 64
    trial_manifest.write_bytes(_canonical_json(document))

    with pytest.raises(_resolution().ResolutionError, match="generated"):
        _resolution().load_trial_artifact(
            wheel=wheel,
            trial_manifest=trial_manifest,
            source_manifest=source_manifest,
        )


def test_resolution_rejects_an_unstaged_package_member_before_install(
    trial_artifact: tuple[Path, Path, Path, dict[str, str]],
) -> None:
    """Resolver evidence cannot authorize code absent from the staged source tree."""
    wheel, trial_manifest, source_manifest, _identity = trial_artifact
    _rewrite_wheel_member(wheel, "gwexpy_studio/backdoor.py", b"raise RuntimeError\n")
    _refresh_trial_manifest_wheel_binding(trial_manifest, wheel)

    with pytest.raises(_resolution().ResolutionError, match="payload"):
        _resolution().load_trial_artifact(
            wheel=wheel,
            trial_manifest=trial_manifest,
            source_manifest=source_manifest,
        )


def test_resolution_rejects_a_trial_wheel_without_the_pure_python_tag(
    trial_artifact: tuple[Path, Path, Path, dict[str, str]],
) -> None:
    """Task3 repeats Task2's pure-wheel invariant before any pip install."""
    wheel, trial_manifest, source_manifest, _identity = trial_artifact
    with zipfile.ZipFile(wheel) as archive:
        wheel_metadata = next(
            name for name in archive.namelist() if name.endswith(".dist-info/WHEEL")
        )
    _rewrite_wheel_member(
        wheel,
        wheel_metadata,
        b"Wheel-Version: 1.0\nRoot-Is-Purelib: false\nTag: cp312-cp312-manylinux\n",
    )
    _refresh_trial_manifest_wheel_binding(trial_manifest, wheel)

    with pytest.raises(_resolution().ResolutionError, match="purelib"):
        _resolution().load_trial_artifact(
            wheel=wheel,
            trial_manifest=trial_manifest,
            source_manifest=source_manifest,
        )


def test_resolution_rejects_a_trial_wheel_filename_without_py3_none_any(
    trial_artifact: tuple[Path, Path, Path, dict[str, str]],
) -> None:
    """The public trial asset cannot claim an architecture-specific wheel name."""
    wheel, trial_manifest, source_manifest, _identity = trial_artifact
    replacement = wheel.with_name(
        wheel.name.replace("py3-none-any", "cp312-cp312-linux_x86_64")
    )
    wheel.rename(replacement)
    document = json.loads(trial_manifest.read_text(encoding="utf-8"))
    document["wheel_filename"] = replacement.name
    trial_manifest.write_bytes(_canonical_json(document))

    with pytest.raises(_resolution().ResolutionError, match="py3-none-any"):
        _resolution().load_trial_artifact(
            wheel=replacement,
            trial_manifest=trial_manifest,
            source_manifest=source_manifest,
        )


def test_resolution_rejects_a_generated_version_file_that_disagrees_with_trial(
    trial_artifact: tuple[Path, Path, Path, dict[str, str]],
) -> None:
    """The staged version delta must repeat the trial version in its module."""
    wheel, trial_manifest, source_manifest, _identity = trial_artifact
    _rewrite_wheel_member(
        wheel,
        "gwexpy_studio/_version.py",
        b'__version__ = "0.0.0"\n',
    )
    _refresh_trial_manifest_wheel_binding(trial_manifest, wheel)
    _refresh_staging_manifest(trial_manifest, wheel)

    with pytest.raises(_resolution().ResolutionError, match="generated version"):
        _resolution().load_trial_artifact(
            wheel=wheel,
            trial_manifest=trial_manifest,
            source_manifest=source_manifest,
        )


def test_resolution_rejects_an_unreviewed_generated_io_policy(
    trial_artifact: tuple[Path, Path, Path, dict[str, str]],
) -> None:
    """The generated policy must remain the reviewed Tier A CSV-read set."""
    wheel, trial_manifest, source_manifest, _identity = trial_artifact
    _rewrite_wheel_member(
        wheel,
        "gwexpy_studio/assets/io-capabilities.json",
        _canonical_json({"schema_version": 1, "entries": []}),
    )
    _refresh_trial_manifest_wheel_binding(trial_manifest, wheel)
    _refresh_staging_manifest(trial_manifest, wheel)

    with pytest.raises(_resolution().ResolutionError, match="capability policy"):
        _resolution().load_trial_artifact(
            wheel=wheel,
            trial_manifest=trial_manifest,
            source_manifest=source_manifest,
        )


def test_resolution_rejects_build_id_and_version_with_different_ci_fields(
    trial_artifact: tuple[Path, Path, Path, dict[str, str]],
) -> None:
    """Version metadata must repeat the exact date, run, and attempt from Build ID."""
    wheel, trial_manifest, source_manifest, identity = trial_artifact
    inconsistent = {
        **identity,
        "version": "0.1.0a1+trial.p.gabcdef0.20260908.r1.a1",
    }
    _write_installable_trial_wheel(wheel, inconsistent)
    document = json.loads(trial_manifest.read_text(encoding="utf-8"))
    document["version"] = inconsistent["version"]
    document["wheel_sha256"] = _sha256(wheel.read_bytes())
    trial_manifest.write_bytes(_canonical_json(document))

    with pytest.raises(_resolution().ResolutionError, match="build ID"):
        _resolution().load_trial_artifact(
            wheel=wheel,
            trial_manifest=trial_manifest,
            source_manifest=source_manifest,
        )


def test_resolution_rejects_missing_binary_hash_and_phase_closure_drift(
    trial_artifact: tuple[Path, Path, Path, dict[str, str]],
) -> None:
    """A missing artifact hash or a changed fresh closure cannot become evidence."""
    wheel, trial_manifest, source_manifest, _identity = trial_artifact
    artifact = _resolution().load_trial_artifact(
        wheel=wheel,
        trial_manifest=trial_manifest,
        source_manifest=source_manifest,
    )
    report = {
        "install": [
            {
                "download_info": {
                    "archive_info": {"hashes": {"sha256": artifact.wheel_sha256}},
                    "url": f"file://{wheel}",
                },
                "is_direct": True,
                "is_yanked": False,
                "metadata": {
                    "name": "gwexpy-studio",
                    "version": artifact.version,
                },
            },
            {
                "download_info": {
                    "archive_info": {},
                    "url": "https://files.pythonhosted.org/packages/numpy-2.3.1.whl",
                },
                "is_direct": False,
                "is_yanked": False,
                "metadata": {"name": "numpy", "version": "2.3.1"},
            },
        ],
        "version": "1",
    }

    with pytest.raises(_resolution().ResolutionError, match="SHA-256"):
        _resolution().runtime_constraints(report, artifact=artifact)


def test_resolution_rejects_different_second_fresh_closure(
    trial_artifact: tuple[Path, Path, Path, dict[str, str]],
) -> None:
    """The re-install may not silently resolve a different transitive runtime."""
    wheel, trial_manifest, source_manifest, _identity = trial_artifact
    artifact = _resolution().load_trial_artifact(
        wheel=wheel,
        trial_manifest=trial_manifest,
        source_manifest=source_manifest,
    )

    def report(numpy_version: str) -> dict[str, object]:
        return {
            "install": [
                {
                    "download_info": {
                        "archive_info": {"hashes": {"sha256": artifact.wheel_sha256}},
                        "url": f"file://{wheel}",
                    },
                    "is_direct": True,
                    "is_yanked": False,
                    "metadata": {
                        "name": "gwexpy-studio",
                        "version": artifact.version,
                    },
                },
                {
                    "download_info": {
                        "archive_info": {"hashes": {"sha256": "a" * 64}},
                        "url": (
                            "https://files.pythonhosted.org/packages/"
                            f"numpy-{numpy_version}-cp312-cp312-manylinux.whl"
                        ),
                    },
                    "is_direct": False,
                    "is_yanked": False,
                    "metadata": {"name": "numpy", "version": numpy_version},
                },
            ],
            "pip_version": "25.3",
            "version": "1",
        }

    def inspect(numpy_version: str) -> dict[str, object]:
        return {
            "installed": [
                {
                    "metadata": {
                        "name": "gwexpy-studio",
                        "version": artifact.version,
                    }
                },
                {"metadata": {"name": "numpy", "version": numpy_version}},
            ],
            "pip_version": "25.3",
            "version": "1",
        }

    with pytest.raises(
        _resolution().ResolutionError, match="different runtime closure"
    ):
        _resolution().build_resolution_projection(
            artifact=artifact,
            machine="x86_64",
            glibc_version="2.39",
            os_runtime=_os_runtime(),
            python_version="3.12.12",
            pip_version="25.3",
            phase_one_report=report("2.3.1"),
            phase_one_inspect=inspect("2.3.1"),
            phase_two_report=report("2.3.2"),
            phase_two_inspect=inspect("2.3.2"),
        )


def test_resolution_rejects_a_different_second_installed_environment(
    trial_artifact: tuple[Path, Path, Path, dict[str, str]],
) -> None:
    """Both fresh installs must retain the same complete installed-package set."""
    wheel, trial_manifest, source_manifest, _identity = trial_artifact
    artifact = _resolution().load_trial_artifact(
        wheel=wheel,
        trial_manifest=trial_manifest,
        source_manifest=source_manifest,
    )
    report = {
        "install": [
            {
                "download_info": {
                    "archive_info": {"hashes": {"sha256": artifact.wheel_sha256}},
                    "url": f"file://{wheel}",
                },
                "is_direct": True,
                "is_yanked": False,
                "metadata": {
                    "name": "gwexpy-studio",
                    "version": artifact.version,
                },
            }
        ],
        "pip_version": "25.3",
        "version": "1",
    }
    phase_one_inspect = {
        "installed": [
            {
                "metadata": {
                    "name": "gwexpy-studio",
                    "version": artifact.version,
                }
            }
        ],
        "pip_version": "25.3",
        "version": "1",
    }
    phase_two_inspect = {
        **phase_one_inspect,
        "installed": [
            *phase_one_inspect["installed"],
            {"metadata": {"name": "unexpected-extra", "version": "1.0"}},
        ],
    }

    with pytest.raises(_resolution().ResolutionError, match="installed environment"):
        _resolution().build_resolution_projection(
            artifact=artifact,
            machine="x86_64",
            glibc_version="2.39",
            os_runtime=_os_runtime(),
            python_version="3.12.12",
            pip_version="25.3",
            phase_one_report=report,
            phase_one_inspect=phase_one_inspect,
            phase_two_report=report,
            phase_two_inspect=phase_two_inspect,
        )


def test_resolution_runs_the_installed_gate_with_an_isolated_phase_two_python(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The technical gate sees only the phase-two installed wheel environment."""
    checkout = tmp_path / "public-p"
    work_root = tmp_path / "private-work"
    sealed_gate = work_root / "installed-trial-technical-gate.py"
    checkout.mkdir()
    work_root.mkdir()
    sealed_gate.write_text("sealed gate\n", encoding="utf-8")
    seen: dict[str, object] = {}

    def fake_run(
        command: object,
        *,
        cwd: Path,
        env: dict[str, str],
        **_kwargs: object,
    ) -> SimpleNamespace:
        values = tuple(command)
        seen["command"] = values
        seen["cwd"] = cwd
        seen["env"] = env
        result = Path(values[values.index("--result") + 1])
        checks = {name: True for name in _gate()._CHECK_NAMES}
        result.write_bytes(
            _gate().gate_result_json(
                checks,
                installed={
                    "architecture": "x86_64",
                    "build_id": "P-abcdef0-20260907-r1-a1",
                    "python_version": "3.12.12",
                    "source_sha": "abcdef0123456789abcdef0123456789abcdef01",
                    "version": "0.1.0a1+trial.p.gabcdef0.20260907.r1.a1",
                },
            )
        )
        return SimpleNamespace(returncode=0)

    monkeypatch.setenv("GWEXPY_STUDIO_IO_CAPABILITIES", "/forged/policy.json")
    monkeypatch.setattr(_resolution().subprocess, "run", fake_run)

    gate_fd = os.open(sealed_gate, os.O_RDONLY)
    try:
        result = _resolution().run_installed_technical_gate(
            python=tmp_path / "phase-two" / "bin" / "python",
            gate_fd=gate_fd,
            checkout_root=checkout,
            work_root=work_root,
        )
    finally:
        os.close(gate_fd)

    assert result["status"] == "passed"
    assert tuple(seen["command"])[1:3] == ("-I", "-c")
    assert tuple(seen["command"])[4] == str(gate_fd)
    assert seen["cwd"] == work_root
    assert "GWEXPY_STUDIO_IO_CAPABILITIES" not in seen["env"]


def test_resolution_rejects_an_unsafe_gate_descriptor(
    tmp_path: Path,
) -> None:
    """The resolved process cannot substitute a standard stream for P bytes."""
    checkout = tmp_path / "public-p"
    work_root = tmp_path / "private-work"
    checkout.mkdir()
    work_root.mkdir()

    with pytest.raises(_resolution().ResolutionError, match="descriptor"):
        _resolution().run_installed_technical_gate(
            python=tmp_path / "phase-two" / "bin" / "python",
            gate_fd=2,
            checkout_root=checkout,
            work_root=work_root,
        )


def test_capture_reinstalls_in_two_fresh_venvs_and_persists_redacted_evidence(
    trial_artifact: tuple[Path, Path, Path, dict[str, str]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A real binary-only install records gate identity for the active 3.12 patch."""
    wheel, trial_manifest, source_manifest, identity, checkout = (
        _bind_trial_artifact_to_checkout(trial_artifact, tmp_path)
    )
    expected_machine = _resolution()._machine()
    expected_python_version = sys.version.split()[0]
    monkeypatch.setattr(
        _resolution(),
        "run_installed_technical_gate",
        lambda **_kwargs: {
            "architecture": expected_machine,
            "checks": {name: True for name in _gate()._CHECK_NAMES},
            "installed": {
                "build_id": identity["build_id"],
                "source_sha": identity["source_sha"],
                "version": identity["version"],
            },
            "python_version": expected_python_version,
            "schema": 2,
            "status": "passed",
        },
    )
    monkeypatch.setattr(
        _resolution(),
        "capture_os_runtime",
        lambda machine, *, cwd: _os_runtime(machine),
        raising=False,
    )

    constraints, resolution = _resolution().capture_trial_resolution(
        wheel=wheel,
        trial_manifest=trial_manifest,
        source_manifest=source_manifest,
        staging_manifest=trial_manifest.with_name("STAGING-MANIFEST.json"),
        checkout_root=checkout,
        output_directory=tmp_path / "captured",
    )
    document = json.loads(resolution.read_text(encoding="utf-8"))
    encoded = resolution.read_text(encoding="utf-8")

    runtime_artifacts = document["runtime_artifacts"]
    assert isinstance(runtime_artifacts, list)
    assert runtime_artifacts
    assert {artifact["name"] for artifact in runtime_artifacts} >= {
        "astropy",
        "gwexpy",
        "gwpy",
        "matplotlib",
        "numpy",
        "pyside6-essentials",
        "scipy",
    }
    assert constraints.read_text(encoding="utf-8").splitlines() == [
        f"{artifact['name']}=={artifact['version']}" for artifact in runtime_artifacts
    ]
    assert {path.name for path in (tmp_path / "captured").iterdir()} == {
        constraints.name,
        resolution.name,
    }
    assert document["phase_one"]["artifacts"] == document["phase_two"]["artifacts"]
    assert runtime_artifacts == [
        artifact
        for artifact in document["phase_one"]["artifacts"]
        if artifact["name"] != "gwexpy-studio"
    ]
    assert document["phase_one"]["pip_report_sha256"] != ""
    assert document["phase_one"]["pip_inspect_sha256"] != ""
    assert document["schema"] == 2
    assert document["os_runtime"] == _os_runtime(expected_machine)
    assert document["technical_gate"] == {
        "architecture": expected_machine,
        "checks": {name: True for name in _gate()._CHECK_NAMES},
        "installed": {
            "build_id": identity["build_id"],
            "source_sha": identity["source_sha"],
            "version": identity["version"],
        },
        "python_version": expected_python_version,
        "schema": 2,
        "status": "passed",
    }
    assert document["staging_manifest_sha256"] == _sha256(
        trial_manifest.with_name("STAGING-MANIFEST.json").read_bytes()
    )
    assert str(tmp_path) not in encoded
    assert "file://" not in encoded
    assert "trial_manifest_sha256" not in document


def test_failed_resolution_does_not_publish_an_empty_or_partial_output(
    trial_artifact: tuple[Path, Path, Path, dict[str, str]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failure after identity validation leaves no caller-visible evidence directory."""
    wheel, trial_manifest, source_manifest, _identity, checkout = (
        _bind_trial_artifact_to_checkout(trial_artifact, tmp_path)
    )
    output = tmp_path / "must-not-appear"

    def fail_install(*_arguments: object, **_kwargs: object) -> None:
        raise _resolution().ResolutionError("simulated resolver failure")

    monkeypatch.setattr(_resolution(), "_run", fail_install)
    monkeypatch.setattr(
        _resolution(),
        "capture_os_runtime",
        lambda machine, *, cwd: _os_runtime(machine),
        raising=False,
    )

    with pytest.raises(_resolution().ResolutionError, match="simulated"):
        _resolution().capture_trial_resolution(
            wheel=wheel,
            trial_manifest=trial_manifest,
            source_manifest=source_manifest,
            staging_manifest=trial_manifest.with_name("STAGING-MANIFEST.json"),
            checkout_root=checkout,
            output_directory=output,
        )

    assert not output.exists()


def test_capture_seals_the_verified_wheel_before_pip_can_reopen_its_path(
    trial_artifact: tuple[Path, Path, Path, dict[str, str]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both fresh phases consume a private copy, never the mutable input path."""
    wheel, trial_manifest, source_manifest, _identity, checkout = (
        _bind_trial_artifact_to_checkout(trial_artifact, tmp_path)
    )
    verified_bytes = wheel.read_bytes()

    def interrupt_after_seal(command: object, **_kwargs: object) -> None:
        staged_wheel = Path(tuple(command)[-1])
        assert staged_wheel.name == wheel.name
        assert staged_wheel != wheel
        assert staged_wheel.read_bytes() == verified_bytes
        wheel.write_bytes(b"replacement after verification")
        raise _resolution().ResolutionError("intentional interruption")

    monkeypatch.setattr(_resolution(), "_run", interrupt_after_seal)
    monkeypatch.setattr(
        _resolution(),
        "capture_os_runtime",
        lambda machine, *, cwd: _os_runtime(machine),
        raising=False,
    )

    with pytest.raises(_resolution().ResolutionError, match="intentional"):
        _resolution().capture_trial_resolution(
            wheel=wheel,
            trial_manifest=trial_manifest,
            source_manifest=source_manifest,
            staging_manifest=trial_manifest.with_name("STAGING-MANIFEST.json"),
            checkout_root=checkout,
            output_directory=tmp_path / "must-not-appear",
        )


def test_resolution_evidence_is_invisible_until_one_no_replace_publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A caller never observes a directory containing only part of the evidence."""
    checkout = tmp_path / "public-p"
    checkout.mkdir()
    output = tmp_path / "published"
    artifacts = {
        "constraints-ubuntu24-x86_64.txt": b"numpy==2.3.1\n",
        "resolution-ubuntu24-x86_64.json": b'{"schema":1}\n',
    }
    publish = _resolution()._publish_evidence
    rename = _resolution()._rename_no_replace

    def observe_then_publish(*arguments: object) -> None:
        assert not output.exists()
        rename(*arguments)

    monkeypatch.setattr(_resolution(), "_rename_no_replace", observe_then_publish)

    publish(artifacts, output, checkout)

    assert {path.name for path in output.iterdir()} == set(artifacts)
    assert {path.read_bytes() for path in output.iterdir()} == set(artifacts.values())


def test_resolution_evidence_refuses_a_target_that_appears_before_publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A competing output is preserved rather than overwritten by evidence."""
    checkout = tmp_path / "public-p"
    checkout.mkdir()
    output = tmp_path / "published"
    artifacts = {
        "constraints-ubuntu24-x86_64.txt": b"numpy==2.3.1\n",
        "resolution-ubuntu24-x86_64.json": b'{"schema":1}\n',
    }
    rename = _resolution()._rename_no_replace

    def create_target_then_publish(*arguments: object) -> None:
        output.mkdir()
        (output / "unrelated.txt").write_text("preserve", encoding="utf-8")
        rename(*arguments)

    monkeypatch.setattr(_resolution(), "_rename_no_replace", create_target_then_publish)

    with pytest.raises(_resolution().ResolutionError, match="appeared"):
        _resolution()._publish_evidence(artifacts, output, checkout)

    assert (output / "unrelated.txt").read_text(encoding="utf-8") == "preserve"
    assert not tuple(tmp_path.glob(".published.stage-*"))


def test_resolution_cli_binds_the_checkout_to_the_capture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The command line provides the P boundary used for safe publication."""
    arguments_seen: dict[str, Path] = {}

    def capture(**arguments: Path) -> tuple[Path, Path]:
        arguments_seen.update(arguments)
        return Path("constraints.txt"), Path("resolution.json")

    monkeypatch.setattr(_resolution(), "capture_trial_resolution", capture)
    checkout = tmp_path / "public-p"

    assert (
        _resolution().main(
            [
                "--wheel",
                str(tmp_path / "trial.whl"),
                "--trial-manifest",
                str(tmp_path / "TRIAL-MANIFEST.json"),
                "--source-manifest",
                str(tmp_path / "SOURCE-MANIFEST.json"),
                "--staging-manifest",
                str(tmp_path / "STAGING-MANIFEST.json"),
                "--checkout",
                str(checkout),
                "--output",
                str(tmp_path / "resolution"),
            ]
        )
        == 0
    )
    assert arguments_seen["checkout_root"] == checkout
    assert arguments_seen["staging_manifest"] == tmp_path / "STAGING-MANIFEST.json"
