"""Deterministic outer trial release archive contracts."""

from __future__ import annotations

import hashlib
import importlib
import stat
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from tests.release.test_trial_bundle import _assemble, _write_trial_inputs

pytestmark = pytest.mark.unit


def _module():
    return importlib.import_module("scripts.package_trial_release")


def _archive(tmp_path: Path) -> tuple[Path, Path, Path]:
    inputs = _write_trial_inputs(tmp_path)
    bundle = tmp_path / "bundle"
    _assemble(inputs, bundle)
    output = tmp_path / "release"
    archive, sidecar = _module().package_trial_release(bundle, output)
    return bundle, archive, sidecar


def test_package_is_byte_deterministic_and_emits_only_archive_and_sidecar(
    tmp_path: Path,
) -> None:
    bundle, archive, sidecar = _archive(tmp_path)
    first = archive.read_bytes()
    assert sorted(path.name for path in archive.parent.iterdir()) == [
        archive.name,
        sidecar.name,
    ]
    assert sidecar.read_text() == (
        f"{hashlib.sha256(first).hexdigest()}  {archive.name}\n"
    )
    with zipfile.ZipFile(archive) as contents:
        infos = contents.infolist()
        assert [info.filename for info in infos] == sorted(
            info.filename for info in infos
        )
        assert all(info.date_time == (1980, 1, 1, 0, 0, 0) for info in infos)
        assert all(info.extra == b"" and info.comment == b"" for info in infos)
        assert all(
            (info.external_attr >> 16) & 0o777777 == 0o100644 for info in infos
        )
        assert all(info.compress_type == zipfile.ZIP_STORED for info in infos)
    second_dir = tmp_path / "release-second"
    archive2, _ = _module().package_trial_release(bundle, second_dir)
    assert archive2.read_bytes() == first


def test_verifier_accepts_valid_archive_and_rejects_tampering(tmp_path: Path) -> None:
    _, archive, sidecar = _archive(tmp_path)
    result = _module().verify_trial_release(archive, sidecar)
    assert result["build"]["id"] in archive.name
    archive.write_bytes(archive.read_bytes() + b"tampered")
    with pytest.raises(_module().TrialReleaseError, match="sidecar|digest"):
        _module().verify_trial_release(archive, sidecar)


@pytest.mark.parametrize("mutation", ["order", "timestamp", "compression", "comment"])
def test_verifier_rejects_noncanonical_outer_zip(
    tmp_path: Path, mutation: str
) -> None:
    _, archive, sidecar = _archive(tmp_path)
    with zipfile.ZipFile(archive) as original:
        entries = [(info, original.read(info)) for info in original.infolist()]
        archive_comment = original.comment
    if mutation == "order":
        entries.reverse()
    elif mutation == "timestamp":
        entries[0][0].date_time = (2020, 1, 1, 0, 0, 0)
    elif mutation == "compression":
        entries[0][0].compress_type = zipfile.ZIP_DEFLATED
    else:
        archive_comment = b"noncanonical"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_STORED) as output:
        output.comment = archive_comment
        for info, data in entries:
            output.writestr(info, data)
    sidecar.write_text(
        f"{hashlib.sha256(archive.read_bytes()).hexdigest()}  {archive.name}\n"
    )
    with pytest.raises(_module().TrialReleaseError, match="canonical"):
        _module().verify_trial_release(archive, sidecar)


def test_packaging_uses_validated_byte_snapshot_without_reread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle, _, _ = _archive(tmp_path)
    module = _module()
    original_verify = module.verify_trial_bundle_bytes
    mutated = bundle / "Feedback.ja.md"

    def verify_then_replace(files):
        result = original_verify(files)
        mutated.write_bytes(b"replacement after validation\n")
        return result

    monkeypatch.setattr(module, "verify_trial_bundle_bytes", verify_then_replace)
    archive, _ = module.package_trial_release(bundle, tmp_path / "new-release")
    with zipfile.ZipFile(archive) as output:
        feedback_member = next(
            name for name in output.namelist() if name.endswith("/Feedback.ja.md")
        )
        assert output.read(feedback_member).startswith(b"#")


def test_packaging_rejects_invalid_bundle_and_nonfresh_output(tmp_path: Path) -> None:
    module = _module()
    invalid_bundle = tmp_path / "invalid-bundle"
    invalid_bundle.mkdir()
    (invalid_bundle / "unexpected.txt").write_text("bad")
    with pytest.raises(module.TrialReleaseError, match="verification"):
        module.package_trial_release(invalid_bundle, tmp_path / "release")

    valid_root = tmp_path / "valid"
    valid_root.mkdir()
    bundle, _, _ = _archive(valid_root)
    existing = tmp_path / "existing"
    existing.mkdir()
    (existing / "keep").write_text("do not overwrite")
    with pytest.raises(module.TrialReleaseError, match="fresh"):
        module.package_trial_release(bundle, existing)


def test_package_and_verify_clis_succeed_and_fail(tmp_path: Path) -> None:
    bundle_inputs = _write_trial_inputs(tmp_path)
    bundle = tmp_path / "bundle"
    _assemble(bundle_inputs, bundle)
    release = tmp_path / "release"
    python = sys.executable
    package = subprocess.run(
        [
            python,
            "scripts/package_trial_release.py",
            "--bundle",
            str(bundle),
            "--output",
            str(release),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert package.returncode == 0
    verify = subprocess.run(
        [
            python,
            "scripts/verify_trial_release.py",
            "--release-directory",
            str(release),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert verify.returncode == 0
    assert "P-abcdef0-20260907-r1-a1" in verify.stdout
    failed_package = subprocess.run(
        [
            python,
            "scripts/package_trial_release.py",
            "--bundle",
            str(tmp_path / "missing"),
            "--output",
            str(tmp_path / "bad"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert failed_package.returncode != 0
    archive_name = next(
        path.name for path in release.iterdir() if path.name.endswith(".zip")
    )
    (release / archive_name).unlink()
    failed_verify = subprocess.run(
        [
            python,
            "scripts/verify_trial_release.py",
            "--release-directory",
            str(release),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert failed_verify.returncode != 0


def test_directory_verifier_requires_exact_archive_and_sidecar_pair(
    tmp_path: Path,
) -> None:
    _, archive, _ = _archive(tmp_path)
    release = archive.parent
    assert _module().verify_trial_release_directory(release)["schema"] == 3
    (release / "extra.txt").write_bytes(b"unexpected")
    with pytest.raises(_module().TrialReleaseError, match="exactly two"):
        _module().verify_trial_release_directory(release)


@pytest.mark.parametrize("case", ["missing", "wrong-name", "wrong-content"])
def test_directory_verifier_rejects_missing_or_malformed_sidecar(
    tmp_path: Path, case: str
) -> None:
    _, archive, sidecar = _archive(tmp_path)
    sidecar.unlink()
    if case == "wrong-name":
        sidecar = archive.parent / "wrong.sha256"
        sidecar.write_text("0" * 64 + "  " + archive.name + "\n")
    elif case == "wrong-content":
        sidecar = archive.parent / f"{archive.name}.sha256"
        sidecar.write_text("0" * 64 + "  " + archive.name + "\n")
    with pytest.raises(_module().TrialReleaseError):
        _module().verify_trial_release_directory(archive.parent)


def test_directory_verifier_rejects_wrong_top_level_root_and_internal_set(
    tmp_path: Path,
) -> None:
    _, archive, sidecar = _archive(tmp_path)
    with zipfile.ZipFile(archive) as original:
        entries = [(info.filename, original.read(info)) for info in original.infolist()]
    wrong_root = "gwexpy-studio-trial-wrong"
    with zipfile.ZipFile(archive, "w") as output:
        for name, data in entries:
            output.writestr(name.replace(name.split("/", 1)[0], wrong_root), data)
    sidecar.write_text(
        f"{hashlib.sha256(archive.read_bytes()).hexdigest()}  {archive.name}\n"
    )
    with pytest.raises(_module().TrialReleaseError):
        _module().verify_trial_release_directory(archive.parent)


@pytest.mark.parametrize("mutation", ["missing", "extra"])
def test_directory_verifier_rejects_missing_or_extra_internal_bundle_file(
    tmp_path: Path, mutation: str
) -> None:
    _, archive, sidecar = _archive(tmp_path)
    with zipfile.ZipFile(archive) as original:
        entries = [(info, original.read(info)) for info in original.infolist()]
        selected = entries if mutation == "extra" else entries[:-1]
        with zipfile.ZipFile(archive, "w") as output:
            for info, data in selected:
                output.writestr(info, data)
            if mutation == "extra":
                extra = zipfile.ZipInfo(info.filename.rsplit("/", 1)[0] + "/extra.txt")
                extra.create_system = 3
                extra.external_attr = (stat.S_IFREG | 0o644) << 16
                output.writestr(extra, b"extra")
    sidecar.write_text(
        f"{hashlib.sha256(archive.read_bytes()).hexdigest()}  {archive.name}\n"
    )
    with pytest.raises(_module().TrialReleaseError, match="internal|canonical"):
        _module().verify_trial_release_directory(archive.parent)


@pytest.mark.parametrize(
    "name",
    [
        "absolute",
        "parent",
        "backslash",
        "duplicate",
        "symlink",
        "nonregular",
        "wrong-mode",
    ],
)
def test_verifier_rejects_unsafe_outer_members(tmp_path: Path, name: str) -> None:
    _, archive, sidecar = _archive(tmp_path)
    output = tmp_path / f"{name}.zip"
    with zipfile.ZipFile(archive) as original:
        entries = [(info, original.read(info)) for info in original.infolist()]
    root = entries[0][0].filename.split("/", 1)[0]
    if name == "absolute":
        entries.append((zipfile.ZipInfo("/absolute"), b"x"))
    elif name == "parent":
        entries.append((zipfile.ZipInfo(f"{root}/../escape"), b"x"))
    elif name == "backslash":
        entries.append((zipfile.ZipInfo(f"{root}\\escape"), b"x"))
    elif name == "duplicate":
        entries.append(entries[0])
    elif name == "symlink":
        info = zipfile.ZipInfo(f"{root}/link")
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        entries.append((info, b"x"))
    else:
        info, data = entries[0]
        info = zipfile.ZipInfo(info.filename)
        info.create_system = 3
        mode = stat.S_IFCHR | 0o644 if name == "nonregular" else stat.S_IFREG | 0o600
        info.external_attr = mode << 16
        entries[0] = (info, data)
    with zipfile.ZipFile(output, "w") as contents:
        for info, data in entries:
            contents.writestr(info, data)
    sidecar.write_text(
        f"{hashlib.sha256(output.read_bytes()).hexdigest()}  {output.name}\n"
    )
    with pytest.raises(_module().TrialReleaseError):
        _module().verify_trial_release(output, sidecar)
