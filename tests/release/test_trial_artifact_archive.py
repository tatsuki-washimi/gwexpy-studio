"""Contracts for fail-closed extraction of a selected GitHub artifact ZIP."""

from __future__ import annotations

import hashlib
import importlib
import stat
import zipfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


def _extractor():
    """Import lazily so a missing module is a failed test, not a collection error."""
    return importlib.import_module("scripts.extract_trial_artifact")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_zip(path: Path, entries: list[tuple[str, bytes]]) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        for name, content in entries:
            archive.writestr(name, content)


def test_extracts_a_digest_bound_flat_artifact_into_a_new_directory(
    tmp_path: Path,
) -> None:
    """A verified artifact produces exactly its regular, top-level files."""
    archive = tmp_path / "trial-bundle.zip"
    _write_zip(archive, [("one.txt", b"one\n"), ("two.bin", b"\x00\x01")])
    output = tmp_path / "bundle"

    extractor = _extractor()
    digest = extractor.extract_verified_trial_artifact(
        archive=archive,
        output_directory=output,
        expected_digest=f"sha256:{_sha256(archive)}",
    )

    assert digest == _sha256(archive)
    assert {path.name for path in output.iterdir()} == {"one.txt", "two.bin"}
    assert (output / "one.txt").read_bytes() == b"one\n"
    assert (output / "two.bin").read_bytes() == b"\x00\x01"


def test_rejects_a_digest_mismatch_without_creating_output(tmp_path: Path) -> None:
    """No file may be extracted before the immutable artifact digest is bound."""
    archive = tmp_path / "trial-bundle.zip"
    _write_zip(archive, [("bundle.txt", b"trial\n")])
    output = tmp_path / "bundle"

    extractor = _extractor()
    with pytest.raises(extractor.TrialArtifactError, match="digest"):
        extractor.extract_verified_trial_artifact(
            archive=archive,
            output_directory=output,
            expected_digest="sha256:" + "0" * 64,
        )

    assert not output.exists()


@pytest.mark.parametrize(
    ("entries", "unsafe_name"),
    [
        ([("../outside.txt", b"outside")], "path"),
        ([("nested/inside.txt", b"inside")], "path"),
        ([("same.txt", b"one"), ("same.txt", b"two")], "duplicate"),
    ],
)
def test_rejects_unsafe_or_ambiguous_member_names_before_extraction(
    tmp_path: Path,
    entries: list[tuple[str, bytes]],
    unsafe_name: str,
) -> None:
    """ZIP member names cannot escape or make the final asset set ambiguous."""
    archive = tmp_path / "trial-bundle.zip"
    _write_zip(archive, entries)
    output = tmp_path / "bundle"

    extractor = _extractor()
    with pytest.raises(extractor.TrialArtifactError, match=unsafe_name):
        extractor.extract_verified_trial_artifact(
            archive=archive,
            output_directory=output,
            expected_digest=f"sha256:{_sha256(archive)}",
        )

    assert not output.exists()


def test_rejects_a_symlink_member_before_extraction(tmp_path: Path) -> None:
    """An archive member cannot create a symlink in the verified bundle directory."""
    archive = tmp_path / "trial-bundle.zip"
    member = zipfile.ZipInfo("link")
    member.create_system = 3
    member.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(archive, "w") as contents:
        contents.writestr(member, "outside")
    output = tmp_path / "bundle"

    extractor = _extractor()
    with pytest.raises(extractor.TrialArtifactError, match="regular"):
        extractor.extract_verified_trial_artifact(
            archive=archive,
            output_directory=output,
            expected_digest=f"sha256:{_sha256(archive)}",
        )

    assert not output.exists()


def test_removes_output_when_the_archive_changes_after_extraction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A final archive identity check cannot leave a rejected bundle behind."""
    archive = tmp_path / "trial-bundle.zip"
    _write_zip(archive, [("bundle.txt", b"trial\n")])
    output = tmp_path / "bundle"
    extractor = _extractor()
    original = extractor._same_file_status
    checks = 0

    def report_a_final_change(expected, actual):
        nonlocal checks
        checks += 1
        return checks == 1 and original(expected, actual)

    monkeypatch.setattr(extractor, "_same_file_status", report_a_final_change)

    with pytest.raises(extractor.TrialArtifactError, match="changed"):
        extractor.extract_verified_trial_artifact(
            archive=archive,
            output_directory=output,
            expected_digest=f"sha256:{_sha256(archive)}",
        )

    assert not output.exists()
