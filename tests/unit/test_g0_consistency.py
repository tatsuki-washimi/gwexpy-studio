"""Focused ownership-boundary tests for G0 consistency helpers."""

from __future__ import annotations

import os
import uuid
from multiprocessing import shared_memory

from tests.support.g0_consistency import _owned_shm_prefix, _shm_names


def test_owned_shm_prefix_is_unique_and_restores_ambient_environment(
    monkeypatch,
) -> None:
    """A G0 worker scope must not leak its test-only shared-memory prefix."""
    variable = "GWEXPY_STUDIO_SHM_PREFIX"
    monkeypatch.setenv(variable, "ambient-")

    with _owned_shm_prefix() as first:
        assert first.startswith("g0-")
        assert os.environ[variable] == first
        with _owned_shm_prefix() as second:
            assert second.startswith("g0-")
            assert second != first
            assert os.environ[variable] == second
        assert os.environ[variable] == first

    assert os.environ[variable] == "ambient-"


def test_owned_shm_prefix_removes_a_previously_unset_environment_variable(
    monkeypatch,
) -> None:
    """A scope entered without an ambient prefix must restore that absence."""
    variable = "GWEXPY_STUDIO_SHM_PREFIX"
    monkeypatch.delenv(variable, raising=False)

    with _owned_shm_prefix() as prefix:
        assert os.environ[variable] == prefix

    assert variable not in os.environ


def test_owned_shm_names_excludes_unrelated_segments() -> None:
    """Leak checks must observe only the worker scope that owns a segment."""
    with _owned_shm_prefix() as prefix:
        owned = shared_memory.SharedMemory(name=f"{prefix}owned", create=True, size=1)
        unrelated = shared_memory.SharedMemory(
            name=f"unrelated-{uuid.uuid4().hex}", create=True, size=1
        )
        try:
            assert _shm_names(prefix) == {owned.name}
        finally:
            owned.close()
            owned.unlink()
            unrelated.close()
            unrelated.unlink()
