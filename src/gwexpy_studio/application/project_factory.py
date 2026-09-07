"""Construction helpers for new alpha projects."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from importlib.metadata import version

from .._version import __version__
from ..domain.project import Project


def create_alpha_project() -> Project:
    """Create an empty schema-v3 project with current compatibility metadata."""
    now = datetime.now(UTC).isoformat()
    return Project(
        schema_version=3,
        project_id=str(uuid.uuid4()),
        created=now,
        modified=now,
        compatibility={
            "studio": __version__,
            "gwexpy": version("gwexpy"),
        },
    )
