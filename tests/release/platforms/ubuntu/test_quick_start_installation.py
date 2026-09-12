"""Executable safety checks for the Ubuntu trial quick-start commands."""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
DOCUMENTS = (
    REPOSITORY_ROOT / "docs/trial/ubuntu/Quick-Start.md",
    REPOSITORY_ROOT / "docs/trial/ubuntu/Quick-Start.ja.md",
)


def _bash_block(document: Path, needle: str) -> str:
    blocks = re.findall(r"```bash\n(.*?)\n```", document.read_text(), re.DOTALL)
    return next(block for block in blocks if needle in block)


def _run_block(
    block: str,
    workdir: Path,
    fake_bin: Path,
    log: Path,
    *,
    prefix: str = "",
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["PATH"] = f"{fake_bin}{os.pathsep}{environment['PATH']}"
    environment["PYTHONNOUSERSITE"] = "1"
    environment.pop("PYTHONPATH", None)
    environment["GWEXPY_TEST_LOG"] = str(log)
    return subprocess.run(
        ["bash", "-c", f"{prefix}{block}"],
        cwd=workdir,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )


def _fake_command(fake_bin: Path, name: str, body: str) -> None:
    command = fake_bin / name
    command.write_text(f"#!/usr/bin/env bash\n{body}\n")
    command.chmod(0o755)


@pytest.fixture
def fake_commands(tmp_path: Path) -> tuple[Path, Path]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log = tmp_path / "commands.log"
    return fake_bin, log


@pytest.mark.parametrize("document", DOCUMENTS, ids=lambda path: path.name)
def test_checksum_failure_stops_before_unzip_and_pip(
    tmp_path: Path, fake_commands: tuple[Path, Path], document: Path
) -> None:
    fake_bin, log = fake_commands
    archive = tmp_path / "gwexpy-studio-trial-ubuntu24-x86_64-test.zip"
    archive.write_bytes(b"not a real archive")
    (tmp_path / f"{archive.name}.sha256").write_text("bad checksum\n")
    _fake_command(
        fake_bin,
        "sha256sum",
        "echo sha256sum >> \"$GWEXPY_TEST_LOG\"; exit 1",
    )
    _fake_command(fake_bin, "unzip", "echo unzip >> \"$GWEXPY_TEST_LOG\"")
    _fake_command(fake_bin, "python", "echo python >> \"$GWEXPY_TEST_LOG\"")

    verification = _bash_block(document, "sha256sum -c \"$sidecar\"")
    install = _bash_block(document, "python -m pip install --only-binary")
    result = _run_block(verification + "\n" + install, tmp_path, fake_bin, log)

    assert result.returncode != 0
    assert log.read_text().splitlines() == ["sha256sum"]


@pytest.mark.parametrize(
    "layout",
    ["missing", "multiple"],
)
@pytest.mark.parametrize("document", DOCUMENTS, ids=lambda path: path.name)
def test_archive_cardinality_stops_before_unzip_and_pip(
    tmp_path: Path,
    fake_commands: tuple[Path, Path],
    document: Path,
    layout: str,
) -> None:
    fake_bin, log = fake_commands
    prefix = "gwexpy-studio-trial-ubuntu24-x86_64-"
    if layout == "multiple":
        for suffix in ("one", "two"):
            (tmp_path / f"{prefix}{suffix}.zip").write_bytes(b"archive")
        (tmp_path / f"{prefix}one.zip.sha256").write_text("checksum\n")
    else:
        (tmp_path / f"{prefix}one.zip.sha256").write_text("checksum\n")
    _fake_command(fake_bin, "unzip", "echo unzip >> \"$GWEXPY_TEST_LOG\"")
    _fake_command(fake_bin, "python", "echo python >> \"$GWEXPY_TEST_LOG\"")

    verification = _bash_block(document, "archives=(gwexpy-studio-trial")
    install = _bash_block(document, "python -m pip install --only-binary")
    result = _run_block(verification + "\n" + install, tmp_path, fake_bin, log)

    assert result.returncode != 0
    assert not log.exists()


@pytest.mark.parametrize("wheel_count", [0, 2], ids=["missing", "multiple"])
@pytest.mark.parametrize("document", DOCUMENTS, ids=lambda path: path.name)
def test_wheel_cardinality_stops_before_pip(
    tmp_path: Path,
    fake_commands: tuple[Path, Path],
    document: Path,
    wheel_count: int,
) -> None:
    fake_bin, log = fake_commands
    (tmp_path / "constraints-ubuntu24-x86_64.txt").write_text("constraints\n")
    for index in range(wheel_count):
        (tmp_path / f"gwexpy_studio-{index}.whl").write_bytes(b"wheel")
    _fake_command(fake_bin, "python", "echo python >> \"$GWEXPY_TEST_LOG\"")

    install = _bash_block(document, "wheels=(./gwexpy_studio-*.whl)")
    result = _run_block(install, tmp_path, fake_bin, log)

    assert result.returncode != 0
    assert not log.exists()


@pytest.mark.parametrize("conda_failure", ["existing", "list-error"])
@pytest.mark.parametrize("document", DOCUMENTS, ids=lambda path: path.name)
def test_conda_preflight_stops_before_create(
    tmp_path: Path,
    fake_commands: tuple[Path, Path],
    document: Path,
    conda_failure: str,
) -> None:
    fake_bin, log = fake_commands
    if conda_failure == "existing":
        conda_body = (
            "echo \"conda $*\" >> \"$GWEXPY_TEST_LOG\"\n"
            "if [[ $1 == env && $2 == list ]]; then "
            "printf 'gwexpy-studio-ubuntu24 *\\n'; exit 0; fi\n"
        )
    else:
        conda_body = (
            "echo \"conda $*\" >> \"$GWEXPY_TEST_LOG\"\n"
            "if [[ $1 == env && $2 == list ]]; then "
            "echo list-error >&2; exit 1; fi\n"
        )
    _fake_command(fake_bin, "conda", conda_body)

    block = _bash_block(document, "env_name=gwexpy-studio-ubuntu24")
    result = _run_block(block, tmp_path, fake_bin, log)

    assert result.returncode != 0
    assert log.read_text().splitlines() == ["conda env list"]


@pytest.mark.parametrize("document", DOCUMENTS, ids=lambda path: path.name)
def test_python_cpu_mismatch_stops_before_pip(
    tmp_path: Path, fake_commands: tuple[Path, Path], document: Path
) -> None:
    fake_bin, log = fake_commands
    (tmp_path / "constraints-ubuntu24-x86_64.txt").write_text("constraints\n")
    (tmp_path / "gwexpy_studio-test.whl").write_bytes(b"wheel")
    sitecustomize = tmp_path / "sitecustomize.py"
    sitecustomize.write_text(
        "import platform\n"
        "platform.machine = lambda: 'aarch64'\n"
    )
    real_python = Path(sys.executable)
    _fake_command(
        fake_bin,
        "python",
        "if [[ $1 == - ]]; then "
        f"PYTHONPATH={shlex.quote(str(sitecustomize.parent))} "
        f"exec {shlex.quote(str(real_python))} -; fi\n"
        "echo python >> \"$GWEXPY_TEST_LOG\"",
    )

    download_block = _bash_block(document, "archives=(gwexpy-studio-trial")
    fail_fast = download_block.splitlines()[0] + "\n"
    conda_block = _bash_block(document, "env_name=gwexpy-studio-ubuntu24")
    install_block = _bash_block(document, "wheels=(./gwexpy_studio-*.whl)")
    conda_function = """
conda() {
  if [[ $1 == env && $2 == list ]]; then return 0; fi
  if [[ $1 == create ]]; then echo "conda create" >> "$GWEXPY_TEST_LOG"; return 0; fi
  if [[ $1 == activate ]]; then return 0; fi
  if [[ $1 == --version ]]; then return 0; fi
  return 1
}
"""
    result = _run_block(
        conda_block + "\n" + install_block,
        tmp_path,
        fake_bin,
        log,
        prefix=fail_fast + conda_function,
    )

    assert result.returncode != 0
    assert "AssertionError" in result.stderr
    assert log.read_text().splitlines() == ["conda create"]
