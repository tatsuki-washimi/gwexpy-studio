"""Kernel-enforced termination when a Linux worker's owning thread exits."""

from __future__ import annotations

import ctypes
import multiprocessing
import os
import signal
import sys
import threading


def _exit_after_parent(parent: object) -> None:
    """Terminate a Darwin worker as soon as its multiprocessing parent exits."""
    parent.join()  # type: ignore[attr-defined]
    os._exit(1)


def guard_parent_lifetime() -> bool:
    """Arm owner-death termination and close the pre-installation parent race.

    Linux uses its kernel parent-death signal. Darwin has no equivalent, so a
    daemon watchdog waits on the multiprocessing parent handle and exits the
    worker without running inherited cleanup after that parent disappears.
    In-process test seams have no multiprocessing parent and remain unchanged.
    """
    parent = multiprocessing.parent_process()
    if parent is None:
        return False
    expected_parent = parent.pid
    if sys.platform == "darwin":
        threading.Thread(
            target=_exit_after_parent,
            args=(parent,),
            name="gwexpy-parent-watchdog",
            daemon=True,
        ).start()
        if os.getppid() != expected_parent:
            os._exit(1)
        return True
    if sys.platform != "linux":
        return False
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0:  # PR_SET_PDEATHSIG
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    if os.getppid() != expected_parent:
        os.kill(os.getpid(), signal.SIGKILL)
    return True
