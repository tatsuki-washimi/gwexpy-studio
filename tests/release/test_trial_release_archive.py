"""Deterministic outer trial release archive contracts."""

from __future__ import annotations

import hashlib
import importlib
import stat
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


@pytest.mark.parametrize(
    "name", ["absolute", "parent", "backslash", "duplicate", "symlink"]
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
    else:
        info = zipfile.ZipInfo(f"{root}/link")
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        entries.append((info, b"x"))
    with zipfile.ZipFile(output, "w") as contents:
        for info, data in entries:
            contents.writestr(info, data)
    sidecar.write_text(
        f"{hashlib.sha256(output.read_bytes()).hexdigest()}  {output.name}\n"
    )
    with pytest.raises(_module().TrialReleaseError):
        _module().verify_trial_release(output, sidecar)
