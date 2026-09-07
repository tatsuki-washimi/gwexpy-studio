"""Process-owned advisory locks for projects and recovery generations."""

from __future__ import annotations

import errno
import fcntl
import hashlib
import os
import stat
import weakref
from pathlib import Path

from .. import user_paths

_LOCKS: weakref.WeakSet[FileLock] = weakref.WeakSet()


def state_directory() -> Path:
    """Return the platform's user state directory without creating it."""
    return user_paths.state_directory()


class FileLock:
    """Hold one nonblocking flock only in the process that acquired it."""

    def __init__(self, path: Path) -> None:
        """Prepare a lock whose file must remain present after release."""
        self.path = path
        self._descriptor: int | None = None
        self._owner = os.getpid()
        _LOCKS.add(self)

    @property
    def writable(self) -> bool:
        """Whether this process currently owns the exclusive lock."""
        return self._descriptor is not None and self._owner == os.getpid()

    def acquire(self, *, create: bool = True) -> bool:
        """Acquire without waiting; return false when another owner is alive."""
        if self.writable:
            return True
        if create:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(
            self.path,
            os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK | (os.O_CREAT if create else 0),
            0o600,
        )
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise OSError(errno.EINVAL, "Lock file must be a regular file")
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(descriptor)
            return False
        except BaseException:
            os.close(descriptor)
            raise
        self._descriptor = descriptor
        self._owner = os.getpid()
        return True

    def close(self) -> None:
        """Release this descriptor without unlocking another process's copy."""
        if self._descriptor is not None:
            os.close(self._descriptor)
            self._descriptor = None

    def __del__(self) -> None:
        """Release abandoned descriptors as a last-resort ownership safeguard."""
        self.close()


def _close_inherited_locks() -> None:
    """Prevent forked workers from extending their parent's ownership lifetime."""
    for lock in list(_LOCKS):
        lock.close()


os.register_at_fork(after_in_child=_close_inherited_locks)


class ProjectLock(FileLock):
    """Exclude other writers to a canonical project path across atomic saves."""

    def __init__(self, path: str | Path, *, root: str | Path | None = None) -> None:
        """Use a stable path digest in an injectable user-state lock directory."""
        self.project_path = Path(path).expanduser().resolve()
        digest = hashlib.sha256(os.fsencode(self.project_path)).hexdigest()
        directory = Path(root) if root is not None else state_directory() / "locks"
        super().__init__(directory / f"{digest}.lock")
