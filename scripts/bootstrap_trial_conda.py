"""Download, verify, and install the source-bound Miniforge bootstrap."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import urllib.request
from collections.abc import Sequence
from pathlib import Path
from typing import BinaryIO

try:
    from .trial_toolchain import (
        ToolchainError,
        miniforge_artifact,
        miniforge_download_url,
        sha256_file,
    )
except ImportError:  # pragma: no cover - direct public-script invocation.
    from trial_toolchain import (  # type: ignore[no-redef]
        ToolchainError,
        miniforge_artifact,
        miniforge_download_url,
        sha256_file,
    )


class BootstrapError(RuntimeError):
    """Raised when a pinned Miniforge bootstrap cannot be trusted or installed."""


def _copy_download(response: BinaryIO, destination: Path) -> None:
    """Write one fresh installer without following a preexisting path."""
    if destination.exists() or destination.is_symlink():
        raise BootstrapError("installer destination already exists")
    try:
        with destination.open("xb") as stream:
            while chunk := response.read(1024 * 1024):
                stream.write(chunk)
    except OSError as exc:
        raise BootstrapError("Miniforge installer cannot be downloaded") from exc


def download_installer(
    *, destination: Path, system: str, architecture: str
) -> Path:
    """Download the official installer bytes to a new regular file."""
    try:
        with urllib.request.urlopen(
            miniforge_download_url(system=system, architecture=architecture),
            timeout=120,
        ) as response:
            _copy_download(response, destination)
    except (OSError, ToolchainError) as exc:
        raise BootstrapError("Miniforge installer download failed") from exc
    return destination


def install_verified_miniforge(
    *, installer: Path, prefix: Path, system: str, architecture: str
) -> Path:
    """Verify installer bytes before invoking the shell installer."""
    if installer.is_symlink() or not installer.is_file():
        raise BootstrapError("Miniforge installer is not a regular file")
    try:
        expected = miniforge_artifact(system=system, architecture=architecture)[
            "sha256"
        ]
        actual = sha256_file(installer)
    except (OSError, ToolchainError) as exc:
        raise BootstrapError("Miniforge installer cannot be verified") from exc
    if actual != expected:
        raise BootstrapError(
            "Miniforge installer SHA-256 does not match the pinned digest"
        )
    if prefix.exists() or prefix.is_symlink() or not prefix.parent.is_dir():
        raise BootstrapError(
            "Miniforge prefix must be a new child of an existing directory"
        )
    try:
        installer.chmod(installer.stat().st_mode | 0o100)
        subprocess.run(
            [str(installer), "-b", "-p", str(prefix)],
            check=True,
            timeout=600,
            env={**os.environ, "PYTHONNOUSERSITE": "1"},
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise BootstrapError("Miniforge installer failed") from exc
    conda = prefix / "bin" / "conda"
    if conda.is_symlink() or not conda.is_file():
        raise BootstrapError("Miniforge installation did not produce conda")
    return conda


def bootstrap(
    *, prefix: Path, installer: Path | None, system: str, architecture: str
) -> Path:
    """Fetch a pinned installer when needed, then install it once."""
    artifact = miniforge_artifact(system=system, architecture=architecture)
    selected = prefix.parent / artifact["filename"] if installer is None else installer
    if not selected.exists():
        selected = download_installer(
            destination=selected, system=system, architecture=architecture
        )
    return install_verified_miniforge(
        installer=selected,
        prefix=prefix,
        system=system,
        architecture=architecture,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prefix", required=True, type=Path)
    parser.add_argument("--installer", type=Path)
    parser.add_argument("--system", choices=("linux", "darwin"), required=True)
    parser.add_argument(
        "--architecture",
        choices=("x86_64", "aarch64", "arm64"),
        required=True,
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the verified Miniforge bootstrap CLI."""
    args = _parser().parse_args(argv)
    try:
        print(bootstrap(**vars(args)))
    except BootstrapError as exc:
        print(f"bootstrap_trial_conda: error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover - direct public-script invocation.
    raise SystemExit(main())
