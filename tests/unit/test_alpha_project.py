"""Tests for alpha project creation and package version metadata."""

from __future__ import annotations

import importlib
import json
import tomllib
import uuid
from datetime import UTC, datetime
from importlib import metadata, resources
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from jsonschema import Draft202012Validator, FormatChecker

import gwexpy_studio
import gwexpy_studio.application as application
import gwexpy_studio.application.alpha as alpha_module
from gwexpy_studio.domain.project import Project
from gwexpy_studio.session import StudioSession


@pytest.mark.contract("C-APP-026")
def test_studio_version_is_single_sourced_and_matches_installed_metadata() -> None:
    """Build metadata and public imports expose the alpha version source."""
    version_module = importlib.import_module("gwexpy_studio._version")
    expected = "0.1.0a1"

    assert version_module.__version__ == expected
    assert gwexpy_studio.__version__ == expected
    assert metadata.version("gwexpy-studio") == expected

    root = Path(__file__).resolve().parents[2]
    configuration = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    project = configuration["project"]
    assert "version" not in project
    assert project["dynamic"] == ["version"]
    assert project["requires-python"] == ">=3.12,<3.13"
    assert configuration["tool"]["setuptools"]["dynamic"]["version"] == {
        "attr": "gwexpy_studio._version.__version__"
    }


@pytest.mark.contract("C-APP-027")
def test_create_alpha_project_returns_unique_schema_valid_utc_projects() -> None:
    """Each factory call creates an independent schema-v2 project."""
    factory = getattr(application, "create_alpha_project", None)
    assert callable(factory)

    first = factory()
    second = factory()

    assert first.schema_version == second.schema_version == 3
    assert first.project_id != second.project_id
    for project in (first, second):
        project_uuid = uuid.UUID(project.project_id)
        assert project_uuid.version == 4
        assert project_uuid.variant == uuid.RFC_4122
        assert project.created == project.modified
        created = datetime.fromisoformat(project.created)
        assert created.tzinfo is not None
        assert created.utcoffset() == UTC.utcoffset(created)
        assert project.compatibility == {
            "studio": gwexpy_studio.__version__,
            "gwexpy": metadata.version("gwexpy"),
        }

        schema_text = (
            resources.files("gwexpy_studio")
            .joinpath("schemas/project-v3.schema.json")
            .read_text(encoding="utf-8")
        )
        validator = Draft202012Validator(
            json.loads(schema_text), format_checker=FormatChecker()
        )
        validator.validate(project.to_dict())


@pytest.mark.contract("C-APP-028")
def test_alpha_controller_uses_alpha_project_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The controller delegates default project construction to the factory."""
    project = Project(
        schema_version=1,
        project_id="factory-project",
        created="2026-09-02T00:00:00+00:00",
        modified="2026-09-02T00:00:00+00:00",
        compatibility={"studio": "test", "gwexpy": "test"},
    )
    factory = MagicMock(return_value=project)
    monkeypatch.setattr(alpha_module, "create_alpha_project", factory)

    controller = alpha_module.AlphaController(session=MagicMock(spec=StudioSession))

    factory.assert_called_once_with()
    assert controller.project is project
