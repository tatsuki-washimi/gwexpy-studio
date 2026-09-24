"""Public installation-guide assets and links."""

from __future__ import annotations

import struct
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
IMAGE_DIR = ROOT / "docs" / "images" / "installation"
IMAGE_NAMES = (
    "01-welcome.png",
    "02-try-sample.png",
    "03-asd.png",
    "04-reopen-project.png",
)
EXPECTED_IMAGE_SIZE = (1200, 800)


def _png_dimensions(path: Path) -> tuple[int, int]:
    """Read the dimensions from a PNG signature and IHDR chunk."""
    data = path.read_bytes()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise AssertionError(f"not a PNG file: {path}")
    if data[12:16] != b"IHDR":
        raise AssertionError(f"PNG has no IHDR chunk: {path}")
    return struct.unpack(">II", data[16:24])


def test_installation_screenshots_are_present_and_uniform() -> None:
    """The four public guide screenshots exist at the documented size."""
    dimensions = []
    for name in IMAGE_NAMES:
        path = IMAGE_DIR / name
        assert path.is_file(), f"missing installation screenshot: {path}"
        dimensions.append(_png_dimensions(path))

    assert dimensions == [EXPECTED_IMAGE_SIZE] * len(IMAGE_NAMES)


def test_installation_guides_reference_all_screenshots() -> None:
    """English and Japanese guides show the same four visual steps."""
    for name in IMAGE_NAMES:
        reference = f"images/installation/{name}"
        assert reference in (ROOT / "docs" / "installation.md").read_text(
            encoding="utf-8"
        )
        assert reference in (ROOT / "docs" / "installation.ja.md").read_text(
            encoding="utf-8"
        )


def test_readmes_point_to_one_installation_hub() -> None:
    """README files expose the installation hub as the primary entry point."""
    assert "docs/installation.md" in (ROOT / "README.md").read_text(
        encoding="utf-8"
    )
    assert "docs/installation.ja.md" in (ROOT / "README.ja.md").read_text(
        encoding="utf-8"
    )


def test_capture_script_is_public_and_has_help() -> None:
    """The screenshot capture entry point is present for future regeneration."""
    script = ROOT / "scripts" / "capture_installation_screenshots.py"
    assert script.is_file()
    assert "--output-dir" in script.read_text(encoding="utf-8")
    completed = subprocess.run(
        [sys.executable, str(script), "--help"],
        cwd=ROOT,
        capture_output=True,
        check=False,
        text=True,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "--source-root" in completed.stdout
