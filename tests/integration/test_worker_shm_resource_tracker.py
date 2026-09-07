"""Regression tests for the worker-owned shared-memory lifecycle."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from multiprocessing import shared_memory
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration


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
