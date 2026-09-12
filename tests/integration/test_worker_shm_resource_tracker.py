"""Regression tests for the worker-owned shared-memory lifecycle."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from multiprocessing import shared_memory
from pathlib import Path

import pytest

from gwexpy_studio.errors import SharedMemoryError
from gwexpy_studio.worker import shm as shm_module

pytestmark = pytest.mark.integration


def test_darwin_owned_names_fit_kernel_limit_and_keep_namespaces(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Darwin worker names stay bounded while run namespaces remain distinct."""
    monkeypatch.setattr(shm_module, "_platform_is_darwin", lambda: True)
    monkeypatch.setenv("GWEXPY_STUDIO_SHM_PREFIX", "g0123456789abn")
    normal_name = shm_module._owned_shm_name()
    monkeypatch.setenv("GWEXPY_STUDIO_SHM_PREFIX", "g0123456789abr")
    recovery_name = shm_module._owned_shm_name()

    assert normal_name is not None
    assert recovery_name is not None
    assert normal_name.startswith("g0123456789abn")
    assert recovery_name.startswith("g0123456789abr")
    assert normal_name != recovery_name
    assert len(normal_name.encode("ascii")) == 30
    assert len(recovery_name.encode("ascii")) == 30


def test_darwin_owned_name_rejects_prefix_over_kernel_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A long explicit prefix fails closed instead of being silently truncated."""
    monkeypatch.setattr(shm_module, "_platform_is_darwin", lambda: True)
    monkeypatch.setenv("GWEXPY_STUDIO_SHM_PREFIX", "g0123456789abnX")

    with pytest.raises(SharedMemoryError, match="14-byte"):
        shm_module._owned_shm_name()


@pytest.mark.contract("C-SHM-022")
def test_worker_release_does_not_write_resource_tracker_keyerror_to_stderr() -> None:
    """Releasing a worker-owned block must not double-unregister its tracker entry."""
    repo_root = Path(__file__).resolve().parents[2]
    script = textwrap.dedent(
        """
        import numpy as np

        from gwexpy_studio.worker.shm import create_block, release_block

        block = create_block(np.arange(8, dtype=np.float64))
        print(block.name, flush=True)
        release_block(block.name)
        block.shm.close()
        """
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        filter(None, (str(repo_root / "src"), env.get("PYTHONPATH", "")))
    )

    completed = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "multiprocessing.resource_tracker" not in completed.stderr
    assert "KeyError" not in completed.stderr
    name = completed.stdout.strip()
    assert name
    with pytest.raises(FileNotFoundError):
        shared_memory.SharedMemory(name=name)
