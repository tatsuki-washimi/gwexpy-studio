"""Studio-owned logging without a GUI or worker runtime dependency."""

from __future__ import annotations

import logging
import os
import stat
import sys
import threading
from collections.abc import Callable, Collection
from dataclasses import dataclass, field
from logging.handlers import RotatingFileHandler
from pathlib import Path
from threading import RLock
from types import TracebackType
from typing import Any

from .user_paths import state_directory

STUDIO_LOGGER_NAME = "gwexpy_studio"
LOG_FILENAME = "studio.log"
WORKER_LOG_FILENAME = "worker.log"
LOG_MAX_BYTES = 1024 * 1024
LOG_BACKUP_COUNT = 5

_HANDLER_MARKER = "_gwexpy_studio_owned_file_handler"
_HOOK_LOCK = RLock()
_installed_exception_hooks: ExceptionHookRegistration | None = None


def _private_directory_flags() -> int:
    return os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)


def _private_file_flags() -> int:
    return os.O_APPEND | os.O_CREAT | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)


def _existing_file_flags() -> int:
    return os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)


def _open_private_child_directory(parent_fd: int, name: str) -> int:
    """Create and open one Studio-owned child without following a symlink."""
    try:
        os.mkdir(name, mode=0o700, dir_fd=parent_fd)
    except FileExistsError:
        pass
    descriptor = os.open(name, _private_directory_flags(), dir_fd=parent_fd)
    try:
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise NotADirectoryError(name)
        os.fchmod(descriptor, 0o700)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _ensure_private_log_directory(directory: Path) -> None:
    """Create Studio state/log leaves without changing an arbitrary parent."""
    studio_state = directory.parent
    xdg_state_home = studio_state.parent
    if directory.name != "logs" or studio_state.name != "gwexpy-studio":
        raise ValueError("Studio logs must live below the XDG state leaf")
    # The XDG base may itself be a legitimate symlink or externally managed
    # directory.  Create it if missing, but never chmod it.  From the Studio
    # leaf down, use dirfd plus O_NOFOLLOW so a redirected state leaf cannot
    # cause permissions or logs to be written elsewhere.
    xdg_state_home.mkdir(parents=True, exist_ok=True)
    parent_descriptor = os.open(
        xdg_state_home, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    )
    try:
        state_descriptor = _open_private_child_directory(
            parent_descriptor, studio_state.name
        )
        try:
            log_descriptor = _open_private_child_directory(
                state_descriptor, directory.name
            )
            os.close(log_descriptor)
        finally:
            os.close(state_descriptor)
    finally:
        os.close(parent_descriptor)


def _secure_existing_regular_log_file(path: Path) -> None:
    """Set owner-only mode on one known log name without following links."""
    try:
        descriptor = os.open(path, _existing_file_flags())
    except FileNotFoundError:
        return
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError(f"Studio log path is not a regular file: {path.name}")
        os.fchmod(descriptor, 0o600)
    finally:
        os.close(descriptor)


def _secure_known_log_files(target: Path) -> None:
    """Repair only the base file and bounded rotation names for one handler."""
    _secure_existing_regular_log_file(target)
    for index in range(1, LOG_BACKUP_COUNT + 1):
        _secure_existing_regular_log_file(target.with_name(f"{target.name}.{index}"))


class _PrivateRotatingFileHandler(RotatingFileHandler):
    """A rotating handler that creates and retains owner-only log files."""

    def _open(self):
        descriptor = os.open(self.baseFilename, _private_file_flags(), 0o600)
        try:
            os.fchmod(descriptor, 0o600)
            return os.fdopen(
                descriptor,
                self.mode,
                encoding=self.encoding,
                errors=self.errors,
            )
        except BaseException:
            os.close(descriptor)
            raise


def log_directory() -> Path:
    """Return the Studio log directory without creating it."""
    return state_directory() / "logs"


def _filename_path(filename: str) -> Path:
    candidate = Path(filename)
    if candidate.name != filename or filename in {"", ".", ".."}:
        raise ValueError("Log filename must be a simple filename")
    return candidate


def _owned_handlers(logger: logging.Logger) -> list[logging.Handler]:
    return [
        handler
        for handler in logger.handlers
        if getattr(handler, _HANDLER_MARKER, False)
    ]


def _remove_owned_handlers(logger: logging.Logger) -> None:
    for handler in _owned_handlers(logger):
        logger.removeHandler(handler)
        handler.close()


def owned_studio_log_handlers(
    *, logger_name: str = STUDIO_LOGGER_NAME
) -> frozenset[logging.Handler]:
    """Return the current Studio-owned handlers for one named logger.

    A nested runtime integration can snapshot this set before setup and close
    only handlers it added itself.
    """
    return frozenset(_owned_handlers(logging.getLogger(logger_name)))


def configure_studio_logging(
    *,
    logger_name: str = STUDIO_LOGGER_NAME,
    filename: str = LOG_FILENAME,
) -> logging.Logger:
    """Create the Studio rotating file logger on explicit runtime setup.

    This function is deliberately the first operation that creates the log
    directory.  Path resolution remains side-effect free for startup probes.
    """
    target = log_directory() / _filename_path(filename)
    _ensure_private_log_directory(target.parent)
    _secure_known_log_files(target)

    logger = logging.getLogger(logger_name)
    matching_handler = next(
        (
            handler
            for handler in _owned_handlers(logger)
            if isinstance(handler, _PrivateRotatingFileHandler)
            and Path(handler.baseFilename) == target
        ),
        None,
    )
    if matching_handler is None:
        _remove_owned_handlers(logger)
        handler = _PrivateRotatingFileHandler(
            target,
            maxBytes=LOG_MAX_BYTES,
            backupCount=LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
        setattr(handler, _HANDLER_MARKER, True)
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(process)d %(levelname)s %(name)s: %(message)s"
            )
        )
        logger.addHandler(handler)

    logger.setLevel(logging.INFO)
    logger.propagate = False
    return logger


def configure_worker_logging() -> logging.Logger:
    """Configure a separate worker log without importing worker code.

    A future worker entry point can call this after process bootstrap.  It is
    intentionally separate from Qt application initialization.
    """
    return configure_studio_logging(
        logger_name=f"{STUDIO_LOGGER_NAME}.worker",
        filename=WORKER_LOG_FILENAME,
    )


def shutdown_studio_logging(
    *,
    logger_name: str = STUDIO_LOGGER_NAME,
    owned_handlers: Collection[logging.Handler] | None = None,
) -> None:
    """Close Studio handlers, optionally restricted to one caller's snapshot."""
    logger = logging.getLogger(logger_name)
    if owned_handlers is None:
        _remove_owned_handlers(logger)
        return
    for handler in owned_handlers:
        is_owned = getattr(handler, _HANDLER_MARKER, False)
        if handler not in logger.handlers or not is_owned:
            continue
        logger.removeHandler(handler)
        handler.close()


def make_qt_message_handler(
    logger: logging.Logger | None = None,
) -> Callable[[object, object, str], None]:
    """Return a PySide-compatible message callback without importing PySide.

    The Qt entry point will install this callback later.  Keeping the adapter
    structural here makes the runtime foundation importable on headless
    systems with no Qt bindings installed.
    """
    destination = (
        logger if logger is not None else logging.getLogger(STUDIO_LOGGER_NAME)
    )

    def handle_qt_message(mode: object, context: object, message: str) -> None:
        del context
        mode_name = str(mode).lower()
        level = (
            logging.ERROR
            if "critical" in mode_name or "fatal" in mode_name
            else logging.WARNING
        )
        destination.log(level, "Qt message: %s", message)

    return handle_qt_message


@dataclass(slots=True)
class ExceptionHookRegistration:
    """Saved standard exception hooks and their installed observing wrappers."""

    logger: logging.Logger
    original_sys_hook: Callable[
        [type[BaseException], BaseException, TracebackType | None], Any
    ]
    original_thread_hook: Callable[[threading.ExceptHookArgs], Any]
    original_unraisable_hook: Callable[[sys.UnraisableHookArgs], Any]
    sys_wrapper: Callable[
        [type[BaseException], BaseException, TracebackType | None], Any
    ]
    thread_wrapper: Callable[[threading.ExceptHookArgs], Any]
    unraisable_wrapper: Callable[[sys.UnraisableHookArgs], Any]
    _active: bool = field(default=True, init=False)

    def restore(self) -> bool:
        """Restore saved hooks when this registration remains active.

        Hooks installed by another component after this registration are left
        intact; restoring observation must not clobber another integration.
        """
        global _installed_exception_hooks
        with _HOOK_LOCK:
            if not self._active:
                return False
            if sys.excepthook is self.sys_wrapper:
                sys.excepthook = self.original_sys_hook
            if threading.excepthook is self.thread_wrapper:
                threading.excepthook = self.original_thread_hook
            if sys.unraisablehook is self.unraisable_wrapper:
                sys.unraisablehook = self.original_unraisable_hook
            self._active = False
            if _installed_exception_hooks is self:
                _installed_exception_hooks = None
            return True


def active_exception_hook_registration() -> ExceptionHookRegistration | None:
    """Return the active Studio hook registration without taking ownership."""
    with _HOOK_LOCK:
        registration = _installed_exception_hooks
        if registration is None or not registration._active:
            return None
        return registration


def _record_exception(
    logger: logging.Logger,
    title: str,
    exception_type: type[BaseException] | None,
    exception: BaseException | None,
    traceback: TracebackType | None,
) -> None:
    """Log an exception while ensuring the original hook is still called."""
    try:
        if exception_type is None or exception is None:
            logger.error("%s (exception details unavailable)", title)
        else:
            logger.error(title, exc_info=(exception_type, exception, traceback))
    except BaseException:
        # Observation must never suppress CPython's standard exception report.
        pass


def install_exception_hooks(
    *, logger: logging.Logger | None = None
) -> ExceptionHookRegistration:
    """Observe unhandled exceptions and then chain to the saved hooks.

    Calling this more than once returns the active registration rather than
    nesting wrappers and duplicating records.
    """
    global _installed_exception_hooks
    with _HOOK_LOCK:
        if (
            _installed_exception_hooks is not None
            and _installed_exception_hooks._active
        ):
            current_registration = _installed_exception_hooks
            if (
                sys.excepthook is current_registration.sys_wrapper
                and threading.excepthook is current_registration.thread_wrapper
                and sys.unraisablehook is current_registration.unraisable_wrapper
            ):
                return current_registration
            # An external integration has displaced at least one wrapper.
            # Restore the wrappers still owned by Studio, then capture the
            # currently installed hooks as the next chain's originals.
            current_registration.restore()

        destination = (
            logger if logger is not None else logging.getLogger(STUDIO_LOGGER_NAME)
        )
        registration: ExceptionHookRegistration

        def observe_sys(
            exception_type: type[BaseException],
            exception: BaseException,
            traceback: TracebackType | None,
        ) -> None:
            _record_exception(
                registration.logger,
                "Unhandled exception",
                exception_type,
                exception,
                traceback,
            )
            registration.original_sys_hook(exception_type, exception, traceback)

        def observe_thread(arguments: threading.ExceptHookArgs) -> None:
            _record_exception(
                registration.logger,
                "Unhandled thread exception",
                arguments.exc_type,
                arguments.exc_value,
                arguments.exc_traceback,
            )
            registration.original_thread_hook(arguments)

        def observe_unraisable(arguments: sys.UnraisableHookArgs) -> None:
            _record_exception(
                registration.logger,
                "Unraisable exception",
                arguments.exc_type,
                arguments.exc_value,
                arguments.exc_traceback,
            )
            registration.original_unraisable_hook(arguments)

        registration = ExceptionHookRegistration(
            logger=destination,
            original_sys_hook=sys.excepthook,
            original_thread_hook=threading.excepthook,
            original_unraisable_hook=sys.unraisablehook,
            sys_wrapper=observe_sys,
            thread_wrapper=observe_thread,
            unraisable_wrapper=observe_unraisable,
        )
        sys.excepthook = observe_sys
        threading.excepthook = observe_thread
        sys.unraisablehook = observe_unraisable
        _installed_exception_hooks = registration
        return registration
