"""Kernel-enforced termination when a Linux worker's owning thread exits."""

from __future__ import annotations

import ctypes
import multiprocessing
import os
import signal
import sys


def guard_parent_lifetime() -> bool:
    """Arm SIGKILL for owner death and close the pre-installation parent race.

    Linux associates this guard with the thread that created the process.
    The GUI's persistent bridge thread therefore must outlive its worker, as
    required by the normal bridge shutdown path. In-process test seams have
    no multiprocessing parent and do not change any process signal behavior.
    """
    parent = multiprocessing.parent_process()
    if sys.platform != "linux" or parent is None:
        return False
    expected_parent = parent.pid
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0:  # PR_SET_PDEATHSIG
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    if os.getppid() != expected_parent:
        os.kill(os.getpid(), signal.SIGKILL)
    return True
