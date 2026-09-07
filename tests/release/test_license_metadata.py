"""Public source-license metadata contracts."""

from __future__ import annotations

import tomllib
from pathlib import Path


def test_project_declares_the_studio_mit_license() -> None:
    """The Studio source has an explicit MIT license file declaration."""
    root = Path(__file__).resolve().parents[2]
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))

    assert project["project"]["license"] == {"file": "LICENSE"}
    assert (root / "LICENSE").read_text(encoding="utf-8").startswith("MIT License\n")
