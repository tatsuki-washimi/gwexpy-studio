"""Executable checks for the WSL2 quick-start host and guest preflight."""

from __future__ import annotations

import os
import re
import shlex
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
DOCUMENTS = (
    REPOSITORY_ROOT / "docs/trial/wsl2/Quick-Start.md",
    REPOSITORY_ROOT / "docs/trial/wsl2/Quick-Start.ja.md",
)


def _preflight_block(document: Path) -> str:
    blocks = re.findall(r"```bash\n(.*?)\n```", document.read_text(), re.DOTALL)
    return next(block for block in blocks if "powershell.exe" in block)


def _fake_command(fake_bin: Path, name: str, body: str) -> None:
    command = fake_bin / name
    command.write_text(f"#!/usr/bin/env bash\n{body}\n", encoding="utf-8")
    command.chmod(0o755)


def _run_preflight(
    document: Path,
    tmp_path: Path,
    *,
    guest_architecture: str,
    host_values: str,
    wslg_available: bool = True,
) -> subprocess.CompletedProcess[str]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    marker = tmp_path / "install-reached"
    _fake_command(
        fake_bin,
        "uname",
        f'if [[ $1 == -m ]]; then printf %s "{guest_architecture}"; else exit 1; fi',
    )
    _fake_command(
        fake_bin,
        "powershell.exe",
        f'printf %s "{host_values}"',
    )
    _fake_command(
        fake_bin,
        "grep",
        "if [[ $* == */proc/sys/kernel/osrelease ]]; then exit 0; fi\n"
        "if [[ $* == */etc/os-release ]]; then exit 0; fi\n"
        "exec /usr/bin/grep \"$@\"",
    )
    environment = os.environ.copy()
    environment["PATH"] = f"{fake_bin}{os.pathsep}{environment['PATH']}"
    environment["WSL_INTEROP"] = "/run/WSL/1_interop"
    environment["WAYLAND_DISPLAY"] = "wayland-0"
    environment.pop("DISPLAY", None)
    block = _preflight_block(document)
    block += f"\nprintf reached > {shlex.quote(str(marker))}"
    # Keep the documented `test` calls intact while making WSLg deterministic.
    wslg_result = "0" if wslg_available else "1"
    prefix = (
        "test() { if [[ $1 == -d && $2 == /mnt/wslg ]]; then return "
        f"{wslg_result}; fi; "
        'builtin test "$@"; }\n'
    )
    return subprocess.run(
        ["bash", "-c", prefix + block],
        cwd=tmp_path,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )


@pytest.mark.parametrize("document", DOCUMENTS, ids=lambda path: path.name)
@pytest.mark.parametrize(
    ("guest_architecture", "host_values", "wslg_available"),
    [
        ("x86_64", "AMD64\r\n22000\r\n1\r\n", True),
        ("aarch64", "ARM64\r\n26100\r\n1\r\n", True),
    ],
    ids=["amd64-x86_64-crlf", "arm64-aarch64-crlf"],
)
def test_wsl2_preflight_accepts_supported_windows11_pairs(
    tmp_path: Path,
    document: Path,
    guest_architecture: str,
    host_values: str,
    wslg_available: bool,
) -> None:
    result = _run_preflight(
        document,
        tmp_path,
        guest_architecture=guest_architecture,
        host_values=host_values,
        wslg_available=wslg_available,
    )

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "install-reached").read_text().strip() == "reached"


@pytest.mark.parametrize("document", DOCUMENTS, ids=lambda path: path.name)
@pytest.mark.parametrize(
    ("guest_architecture", "host_values", "wslg_available"),
    [
        ("aarch64", "AMD64\r\n22631\r\n1\r\n", True),
        ("x86_64", "ARM64\r\n22631\r\n1\r\n", True),
        ("x86_64", "AMD64\r\n21999\r\n1\r\n", True),
        ("x86_64", "AMD64\r\n22631\r\n2\r\n", True),
        ("x86_64", "AMD64\r\nnot-a-build\r\n1\r\n", True),
        ("x86_64", "AMD64\r\n\r\n1\r\n", True),
        ("x86_64", "\r\n22631\r\n1\r\n", True),
        ("x86_64", "AMD64\r\n22631\r\n1\r\n", False),
    ],
    ids=[
        "host-guest-mismatch",
        "host-guest-mismatch-arm",
        "unsupported-build",
        "server-product-type",
        "nonnumeric-build",
        "empty-build",
        "empty-host-architecture",
        "missing-wslg",
    ],
)
def test_wsl2_preflight_rejects_unsupported_hosts_before_install(
    tmp_path: Path,
    document: Path,
    guest_architecture: str,
    host_values: str,
    wslg_available: bool,
) -> None:
    result = _run_preflight(
        document,
        tmp_path,
        guest_architecture=guest_architecture,
        host_values=host_values,
        wslg_available=wslg_available,
    )

    assert result.returncode != 0
    assert not (tmp_path / "install-reached").exists()
