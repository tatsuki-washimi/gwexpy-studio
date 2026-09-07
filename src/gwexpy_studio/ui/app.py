"""Studio application launcher and lifecycle manager."""

from __future__ import annotations

import json
import multiprocessing
import os
import re
import stat
import sys
from collections.abc import Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import datetime
from importlib import resources
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as distribution_version
from pathlib import Path
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import Qt, qInstallMessageHandler
from PySide6.QtWidgets import QApplication

from ..ops.io_capabilities import (
    CAPABILITY_ENVIRONMENT,
    KNOWN_IO_CLASSES,
    load_capability_manifest,
)
from ..runtime.logging import (
    ExceptionHookRegistration,
    active_exception_hook_registration,
    configure_studio_logging,
    install_exception_hooks,
    make_qt_message_handler,
    owned_studio_log_handlers,
    shutdown_studio_logging,
)
from ..user_paths import bootstrap_matplotlib
from .launch import LauncherUsageError, parse_project_argument

if TYPE_CHECKING:
    from .window import MainWindow


# ``ui.window`` imports PlotCanvas, which imports Matplotlib.  Bootstrap at
# formal-launcher import time so the cache is fixed before that import edge.
bootstrap_matplotlib()


_TRIAL_BUILD_RESOURCE = ("assets", "trial-build.json")
_TRIAL_CAPABILITY_RESOURCE = ("assets", "io-capabilities.json")
_INVALID_TRIAL_CAPABILITY_PATH = "/__gwexpy_studio_invalid_trial_capability__"
_MISSING = object()
_TRIAL_BUILD_ID_ENVIRONMENT = "GWEXPY_STUDIO_BUILD_ID"
_TRIAL_SOURCE_COMMIT_ENVIRONMENT = "GWEXPY_STUDIO_SOURCE_COMMIT"
_TRIAL_BUILD_SCHEMA = 1
_MAX_TRIAL_BUILD_RECORD_BYTES = 16 * 1024
_TRIAL_BUILD_ID = re.compile(
    r"P-([0-9a-f]{7})-([0-9]{8})-r([1-9][0-9]*)-a([1-9][0-9]*)"
)
_TRIAL_SOURCE_SHA = re.compile(r"[0-9a-f]{40}")
_TRIAL_SOURCE_MANIFEST_SHA256 = re.compile(r"[0-9a-f]{64}")
_TRIAL_VERSION = re.compile(
    r"0\.1\.0a1\+trial\.p\.([0-9a-f]{7})\.([0-9]{8})\.r"
    r"([1-9][0-9]*)\.a([1-9][0-9]*)"
)


@dataclass(frozen=True, slots=True)
class _TrialBuildRecord:
    """The strictly validated identity stamped into one generated trial wheel."""

    build_id: str
    source_sha: str
    source_manifest_sha256: str
    version: str


def _discard_qt_message(mode: object, context: object, message: str) -> None:
    """Discard Qt output while private file logging is unavailable."""
    del mode, context, message


def _trial_resource(*parts: str) -> Any:
    """Resolve one package-local trial asset without consulting user paths."""
    return resources.files("gwexpy_studio").joinpath(*parts)


def _unique_trial_record_fields(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    """Reject duplicate fields rather than choosing one provenance claim."""
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("Trial build record has duplicate fields")
    return result


def _reject_trial_record_constant(value: str) -> None:
    """Reject non-finite JSON values from an embedded provenance record."""
    raise ValueError(f"Trial build record has invalid JSON constant {value!r}")


def _read_trial_build_record(path_value: str) -> _TrialBuildRecord:
    """Load one bounded, regular package identity record without symlink follows."""
    if not path_value or not Path(path_value).is_absolute():
        raise ValueError("Trial build record path is invalid")
    flags = (
        os.O_RDONLY
        | os.O_NONBLOCK
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.open(path_value, flags)
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_size > _MAX_TRIAL_BUILD_RECORD_BYTES
        ):
            raise ValueError("Trial build record is not a bounded regular file")
        chunks: list[bytes] = []
        remaining = _MAX_TRIAL_BUILD_RECORD_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        content = b"".join(chunks)
    finally:
        os.close(descriptor)
    if len(content) > _MAX_TRIAL_BUILD_RECORD_BYTES:
        raise ValueError("Trial build record exceeds size limit")
    try:
        document = json.loads(
            content.decode("utf-8"),
            object_pairs_hook=_unique_trial_record_fields,
            parse_constant=_reject_trial_record_constant,
        )
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise ValueError("Trial build record is malformed") from exc
    return _trial_build_record(document)


def _trial_build_record(document: object) -> _TrialBuildRecord:
    """Validate all identity fields and their CI-derived relationship."""
    expected = {
        "build_id",
        "schema",
        "source_manifest_sha256",
        "source_sha",
        "version",
    }
    if type(document) is not dict or set(document) != expected:
        raise ValueError("Trial build record schema is invalid")
    if type(document["schema"]) is not int or document["schema"] != _TRIAL_BUILD_SCHEMA:
        raise ValueError("Trial build record schema is unsupported")
    build_id = document["build_id"]
    source_sha = document["source_sha"]
    source_manifest_sha256 = document["source_manifest_sha256"]
    version = document["version"]
    if (
        type(build_id) is not str
        or type(source_sha) is not str
        or type(source_manifest_sha256) is not str
        or type(version) is not str
    ):
        raise ValueError("Trial build record identity fields are invalid")
    build_match = _TRIAL_BUILD_ID.fullmatch(build_id)
    version_match = _TRIAL_VERSION.fullmatch(version)
    if (
        build_match is None
        or version_match is None
        or _TRIAL_SOURCE_SHA.fullmatch(source_sha) is None
        or _TRIAL_SOURCE_MANIFEST_SHA256.fullmatch(source_manifest_sha256) is None
    ):
        raise ValueError("Trial build record identity is invalid")
    if (
        build_match.groups() != version_match.groups()
        or build_match.group(1) != source_sha[:7]
    ):
        raise ValueError("Trial build record identity is inconsistent")
    try:
        datetime.strptime(build_match.group(2), "%Y%m%d")
    except ValueError as exc:
        raise ValueError("Trial build record date is invalid") from exc
    return _TrialBuildRecord(
        build_id=build_id,
        source_sha=source_sha,
        source_manifest_sha256=source_manifest_sha256,
        version=version,
    )


def _installed_package_version() -> str | None:
    """Return distribution metadata only when it is a normal version string."""
    try:
        candidate = distribution_version("gwexpy-studio")
    except (PackageNotFoundError, OSError, ValueError):
        return None
    return candidate if type(candidate) is str else None


def _is_trial_distribution_version(value: str | None) -> bool:
    """Identify a generated trial independently from mutable package assets."""
    return value is not None and _TRIAL_VERSION.fullmatch(value) is not None


def _restore_trial_environment(original: dict[str, object]) -> None:
    """Restore exactly the inherited values after the Qt application exits."""
    for name, value in original.items():
        if value is _MISSING:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value  # type: ignore[assignment]


def _disable_trial_environment() -> None:
    """Prevent damaged trial identity from inheriting any external assertions."""
    os.environ[CAPABILITY_ENVIRONMENT] = _INVALID_TRIAL_CAPABILITY_PATH
    os.environ.pop(_TRIAL_BUILD_ID_ENVIRONMENT, None)
    os.environ.pop(_TRIAL_SOURCE_COMMIT_ENVIRONMENT, None)


@contextmanager
def _trial_capability_environment() -> Any:
    """Bind a trial launcher to its embedded policy and identity for its lifetime.

    A generated ``trial-build.json`` and the generated distribution version
    independently identify a trial wheel.  Its record and package-local
    capability policy must both validate before the launcher accepts them.  A
    damaged trial never inherits external policy or identity; it uses an
    invalid policy path and hides the identity.  Source mode has neither trial
    marker and keeps the existing development environment untouched.
    """
    original = {
        name: os.environ.get(name, _MISSING)
        for name in (
            CAPABILITY_ENVIRONMENT,
            _TRIAL_BUILD_ID_ENVIRONMENT,
            _TRIAL_SOURCE_COMMIT_ENVIRONMENT,
        )
    }
    resource_stack = ExitStack()
    try:
        installed_version = _installed_package_version()
        try:
            has_trial_record = bool(
                _trial_resource(*_TRIAL_BUILD_RESOURCE).is_file()
            )
        except (AttributeError, OSError, RuntimeError, ValueError, TypeError):
            # A package resource lookup failure is unsafe to interpret as an
            # ordinary source launch: prevent an inherited policy bypass.
            has_trial_record = True
        is_trial = has_trial_record or _is_trial_distribution_version(
            installed_version
        )
        if not is_trial:
            yield
            return

        try:
            record_path = resource_stack.enter_context(
                resources.as_file(_trial_resource(*_TRIAL_BUILD_RESOURCE))
            )
            record = _read_trial_build_record(os.fspath(record_path))
            if installed_version != record.version:
                raise ValueError("Trial build record version does not match package")
            manifest_path = resource_stack.enter_context(
                resources.as_file(_trial_resource(*_TRIAL_CAPABILITY_RESOURCE))
            )
            manifest = load_capability_manifest(
                environ={CAPABILITY_ENVIRONMENT: os.fspath(manifest_path)},
                io_classes=KNOWN_IO_CLASSES,
            )
            if manifest.mode != "frozen":
                raise ValueError("Trial capability policy is invalid")
        except (
            AttributeError,
            OSError,
            RuntimeError,
            ValueError,
            TypeError,
            UnicodeError,
            RecursionError,
        ):
            _disable_trial_environment()
        else:
            os.environ[CAPABILITY_ENVIRONMENT] = os.fspath(manifest_path)
            os.environ[_TRIAL_BUILD_ID_ENVIRONMENT] = record.build_id
            os.environ[_TRIAL_SOURCE_COMMIT_ENVIRONMENT] = record.source_sha
        yield
    finally:
        try:
            resource_stack.close()
        except (OSError, RuntimeError, ValueError, TypeError):
            pass
        _restore_trial_environment(original)


class _LaunchLoggingLifecycle:
    """Own Studio logging integrations for one GUI application lifetime."""

    def __init__(self) -> None:
        """Start with a no-op lifecycle for a safe logging fallback."""
        self._hooks: ExceptionHookRegistration | None = None
        self._qt_handler: Any = None
        self._previous_qt_handler: Any = None
        self._qt_handler_installed = False
        self._owned_log_handlers: frozenset[Any] = frozenset()

    def install_qt_handler(self, handler: Any) -> bool:
        """Install one handler and remember its predecessor if successful."""
        try:
            self._qt_handler = handler
            self._previous_qt_handler = qInstallMessageHandler(handler)
        except Exception:
            return False
        self._qt_handler_installed = True
        return True

    def close(self) -> None:
        """Restore only integrations installed by this launcher invocation."""
        if self._qt_handler_installed:
            try:
                current_qt_handler = qInstallMessageHandler(None)
                if current_qt_handler is self._qt_handler:
                    qInstallMessageHandler(self._previous_qt_handler)
                else:
                    qInstallMessageHandler(current_qt_handler)
            except Exception:
                pass
        if self._hooks is not None:
            try:
                self._hooks.restore()
            except Exception:
                pass
        if self._owned_log_handlers:
            try:
                shutdown_studio_logging(owned_handlers=self._owned_log_handlers)
            except Exception:
                pass


def _configure_launch_logging() -> _LaunchLoggingLifecycle:
    """Set up optional Studio logging without making launch depend on it."""
    lifecycle = _LaunchLoggingLifecycle()
    try:
        existing_handlers = owned_studio_log_handlers()
        logger = configure_studio_logging()
    except Exception:
        lifecycle.install_qt_handler(_discard_qt_message)
        return lifecycle

    try:
        lifecycle._owned_log_handlers = owned_studio_log_handlers() - existing_handlers
    except Exception:
        lifecycle._owned_log_handlers = frozenset()

    existing_hooks = active_exception_hook_registration()
    try:
        registration = install_exception_hooks(logger=logger)
        if registration is not existing_hooks:
            lifecycle._hooks = registration
    except Exception:
        pass
    try:
        lifecycle.install_qt_handler(make_qt_message_handler(logger))
    except Exception:
        lifecycle.install_qt_handler(_discard_qt_message)
    return lifecycle


def create_app(argv: Sequence[str] | None = None) -> tuple[QApplication, MainWindow]:
    """Create and configure single QApplication and MainWindow instances."""
    from .window import MainWindow

    app = QApplication.instance()
    if app is None:
        args = list(argv) if argv is not None else sys.argv
        app = QApplication(args)
        app.setAttribute(Qt.ApplicationAttribute.AA_Use96Dpi, True)
    elif not isinstance(app, QApplication):
        raise TypeError(f"Expected QApplication, got {type(app)}")

    window = MainWindow()
    return app, window


def main(argv: Sequence[str] | None = None) -> int:
    """Run Studio desktop application."""
    multiprocessing.freeze_support()
    arguments = tuple(sys.argv[1:] if argv is None else argv)
    try:
        project_path = parse_project_argument(arguments)
    except LauncherUsageError as error:
        print(f"gwexpy-studio: error: {error}", file=sys.stderr)
        return 2

    logging_lifecycle = _configure_launch_logging()
    try:
        with _trial_capability_environment():
            app, window = create_app((sys.argv[0], *arguments))
            window.show()
            if project_path is None:
                window.initialize_workspace()
            else:
                window.start_initial_workspace(project_path)
            return app.exec()
    finally:
        logging_lifecycle.close()


if __name__ == "__main__":
    sys.exit(main())
