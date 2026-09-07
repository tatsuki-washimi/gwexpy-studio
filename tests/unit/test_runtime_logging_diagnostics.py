"""Contracts for the trial runtime logging and privacy boundary."""

from __future__ import annotations

import json
import logging
import os
import stat
import subprocess
import sys
import threading
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.unit
_TEST_AWS_ACCESS_KEY = "AKIA" + "ABCDEFGHIJKLMNOP"
_TEST_AWS_CAVEAT = f"AWS secret access key {_TEST_AWS_ACCESS_KEY}"


def _flush(logger: logging.Logger) -> None:
    for handler in logger.handlers:
        handler.flush()


@pytest.mark.contract("TLD-001")
def test_log_directory_resolution_does_not_create_xdg_state_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Merely finding the log destination has no persistent side effect."""
    from gwexpy_studio.runtime.logging import log_directory

    state_home = tmp_path / "state"
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))

    assert not state_home.exists()
    assert log_directory() == state_home / "gwexpy-studio" / "logs"
    assert not state_home.exists()


@pytest.mark.contract("TLD-002")
def test_configure_studio_logging_uses_xdg_state_and_rotates(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The Studio-owned file logger has the published retention policy."""
    from gwexpy_studio.runtime.logging import (
        LOG_BACKUP_COUNT,
        LOG_MAX_BYTES,
        configure_studio_logging,
        shutdown_studio_logging,
    )

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state home"))
    logger_name = "gwexpy_studio.tests.rotation"
    previous_umask = os.umask(0o022)
    try:
        logger = configure_studio_logging(logger_name=logger_name)
        handlers = [
            handler
            for handler in logger.handlers
            if isinstance(handler, logging.handlers.RotatingFileHandler)
        ]

        assert len(handlers) == 1
        handler = handlers[0]
        assert handler.baseFilename == str(
            tmp_path / "state home" / "gwexpy-studio" / "logs" / "studio.log"
        )
        assert handler.maxBytes == LOG_MAX_BYTES == 1024 * 1024
        assert handler.backupCount == LOG_BACKUP_COUNT == 5
        log_path = Path(handler.baseFilename)
        assert stat.S_IMODE(log_path.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(log_path.stat().st_mode) == 0o600

        # Lowering the already-configured threshold exercises real rollover
        # without making a unit test write a MiB of diagnostic data.
        handler.maxBytes = 1
        logger.info("first log record")
        logger.info("second log record")
        _flush(logger)
        rotated = Path(f"{handler.baseFilename}.1")
        assert rotated.is_file()
        assert stat.S_IMODE(Path(handler.baseFilename).stat().st_mode) == 0o600
        assert stat.S_IMODE(rotated.stat().st_mode) == 0o600
    finally:
        os.umask(previous_umask)
        shutdown_studio_logging(logger_name=logger_name)


@pytest.mark.contract("TRT-LOG-001")
def test_shutdown_studio_logging_can_close_only_a_caller_owned_handler(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Nested launchers can leave a pre-existing Studio handler untouched."""
    from gwexpy_studio.runtime.logging import (
        configure_studio_logging,
        owned_studio_log_handlers,
        shutdown_studio_logging,
    )

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    logger_name = "gwexpy_studio.tests.handler-ownership"
    try:
        configure_studio_logging(logger_name=logger_name)
        existing_handlers = owned_studio_log_handlers(logger_name=logger_name)
        assert len(existing_handlers) == 1

        shutdown_studio_logging(
            logger_name=logger_name,
            owned_handlers=frozenset(),
        )
        assert owned_studio_log_handlers(logger_name=logger_name) == existing_handlers

        shutdown_studio_logging(
            logger_name=logger_name,
            owned_handlers=existing_handlers,
        )
        assert owned_studio_log_handlers(logger_name=logger_name) == frozenset()
    finally:
        shutdown_studio_logging(logger_name=logger_name)


@pytest.mark.contract("TLD-012")
def test_configure_studio_logging_repairs_existing_rotated_log_permissions(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Pre-existing Studio rotation files cannot retain world-readable modes."""
    from gwexpy_studio.runtime.logging import (
        configure_studio_logging,
        log_directory,
        shutdown_studio_logging,
    )

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    directory = log_directory()
    directory.mkdir(parents=True, mode=0o700)
    rotated = directory / "studio.log.1"
    rotated.write_text("old private path /data/experiment.csv\n", encoding="utf-8")
    rotated.chmod(0o644)
    logger_name = "gwexpy_studio.tests.rotation-repair"

    try:
        configure_studio_logging(logger_name=logger_name)

        assert stat.S_IMODE(rotated.stat().st_mode) == 0o600
    finally:
        shutdown_studio_logging(logger_name=logger_name)


@pytest.mark.contract("TLD-013")
def test_configure_studio_logging_rejects_a_special_existing_log_without_blocking(
    tmp_path: Path,
) -> None:
    """A corrupt FIFO at a Studio log name is rejected rather than opened."""
    if not hasattr(os, "mkfifo"):
        pytest.skip("this platform cannot create a POSIX named pipe")
    state_home = tmp_path / "state"
    log_directory = state_home / "gwexpy-studio" / "logs"
    log_directory.mkdir(parents=True, mode=0o700)
    os.mkfifo(log_directory / "studio.log")
    environment = os.environ | {"XDG_STATE_HOME": str(state_home)}
    probe = "\n".join(
        (
            "from gwexpy_studio.runtime.logging import configure_studio_logging",
            "try:",
            "    configure_studio_logging(logger_name='gwexpy_studio.tests.fifo')",
            "except (OSError, ValueError):",
            "    print('rejected')",
            "else:",
            "    print('configured')",
        )
    )

    try:
        completed = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True,
            check=False,
            env=environment,
            text=True,
            timeout=2,
        )
    except subprocess.TimeoutExpired as exc:
        pytest.fail(f"configure_studio_logging blocked on a FIFO: {exc}")

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == "rejected\n"


@pytest.mark.contract("TLD-014")
def test_configure_studio_logging_rejects_a_symlinked_studio_state_leaf(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A symlink at Studio's own state leaf cannot redirect log permissions."""
    from gwexpy_studio.runtime.logging import (
        configure_studio_logging,
        shutdown_studio_logging,
    )

    state_home = tmp_path / "state"
    unrelated = tmp_path / "unrelated"
    state_home.mkdir()
    unrelated.mkdir()
    (state_home / "gwexpy-studio").symlink_to(unrelated, target_is_directory=True)
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
    logger_name = "gwexpy_studio.tests.symlinked-state"

    try:
        with pytest.raises(OSError):
            configure_studio_logging(logger_name=logger_name)

        assert not (unrelated / "logs").exists()
    finally:
        shutdown_studio_logging(logger_name=logger_name)


@pytest.mark.contract("TLD-003")
def test_exception_hooks_observe_then_chain_and_restore(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Observed exceptions are logged without replacing standard hook behavior."""
    from gwexpy_studio.runtime.logging import (
        active_exception_hook_registration,
        configure_studio_logging,
        install_exception_hooks,
        shutdown_studio_logging,
    )

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    logger_name = "gwexpy_studio.tests.exception-hooks"
    chained: list[str] = []

    def original_sys_hook(
        exception_type: type[BaseException],
        exception: BaseException,
        traceback: object,
    ) -> None:
        del exception_type, exception, traceback
        chained.append("sys")

    def original_thread_hook(arguments: object) -> None:
        del arguments
        chained.append("thread")

    def original_unraisable_hook(arguments: object) -> None:
        del arguments
        chained.append("unraisable")

    monkeypatch.setattr(sys, "excepthook", original_sys_hook)
    monkeypatch.setattr(threading, "excepthook", original_thread_hook)
    monkeypatch.setattr(sys, "unraisablehook", original_unraisable_hook)
    logger = configure_studio_logging(logger_name=logger_name)
    registration = install_exception_hooks(logger=logger)
    replacement = None
    try:
        assert active_exception_hook_registration() is registration
        assert install_exception_hooks(logger=logger) is registration

        try:
            raise ValueError("record this failure")
        except ValueError as error:
            sys.excepthook(type(error), error, error.__traceback__)
            threading.excepthook(
                SimpleNamespace(
                    exc_type=type(error),
                    exc_value=error,
                    exc_traceback=error.__traceback__,
                    thread=None,
                )
            )
            sys.unraisablehook(
                SimpleNamespace(
                    exc_type=type(error),
                    exc_value=error,
                    exc_traceback=error.__traceback__,
                    err_msg=None,
                    object=None,
                )
            )

        _flush(logger)
        assert chained == ["sys", "thread", "unraisable"]
        assert "ValueError: record this failure" in (
            tmp_path / "state" / "gwexpy-studio" / "logs" / "studio.log"
        ).read_text(encoding="utf-8")

        def external_sys_hook(
            exception_type: type[BaseException],
            exception: BaseException,
            traceback: object,
        ) -> None:
            del exception_type, exception, traceback
            chained.append("external-sys")

        # Simulate another integration replacing one hook while Studio still
        # owns the remaining two.  Reinstalling must make a fresh chain rather
        # than returning a stale registration.
        sys.excepthook = external_sys_hook
        replacement = install_exception_hooks(logger=logger)
        assert replacement is not registration
        assert registration.restore() is False
        assert sys.excepthook is replacement.sys_wrapper
        assert threading.excepthook is replacement.thread_wrapper
        assert sys.unraisablehook is replacement.unraisable_wrapper

        try:
            raise KeyError("record the new chain")
        except KeyError as error:
            sys.excepthook(type(error), error, error.__traceback__)

        assert chained[-1:] == ["external-sys"]
        assert replacement.restore() is True
        assert active_exception_hook_registration() is None
        assert sys.excepthook is external_sys_hook
        assert threading.excepthook is original_thread_hook
        assert sys.unraisablehook is original_unraisable_hook
        assert replacement.restore() is False
    finally:
        if replacement is not None:
            replacement.restore()
        registration.restore()
        shutdown_studio_logging(logger_name=logger_name)


@pytest.mark.contract("TLD-004")
def test_diagnostics_are_sanitized_and_exclude_user_supplied_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Copy Diagnostics emits only allowlisted machine-safe fields."""
    from gwexpy_studio.runtime.diagnostics import (
        diagnostics_payload,
        format_diagnostics,
    )

    monkeypatch.setenv("GWEXPY_STUDIO_BUILD_ID", "P-abcdef1-20260906-01")
    monkeypatch.setenv("GWEXPY_STUDIO_SOURCE_COMMIT", "a" * 40)
    payload = diagnostics_payload(
        project_schema_version="/private/project.gwxproj",
        worker_protocol_version="recovery: secret",
        io_capability_digest="/home/user/experiment.csv",
        last_error_code="/data/secret/measurement.hdf5",
    )
    text = format_diagnostics(
        project_schema_version="/private/project.gwxproj",
        worker_protocol_version="recovery: secret",
        io_capability_digest="/home/user/experiment.csv",
        last_error_code="/data/secret/measurement.hdf5",
    )

    assert payload["studio_version"] == "0.1.0a1"
    assert payload["build_id"] == "P-abcdef1-20260906-01"
    assert payload["source_commit"] == "a" * 40
    assert payload["project_schema_version"] == "unknown"
    assert payload["worker_protocol_version"] == "unknown"
    assert payload["io_capability_digest"] == "unknown"
    assert payload["last_error_code"] == "unknown"
    assert "/private/project.gwxproj" not in text
    assert "/home/user/experiment.csv" not in text
    assert "/data/secret/measurement.hdf5" not in text
    assert "recovery: secret" not in text
    assert "log" not in {key.lower() for key in payload}

    enormous = 10**5000
    enormous_payload = diagnostics_payload(
        project_schema_version=enormous,
        worker_protocol_version=enormous,
    )
    assert enormous_payload["project_schema_version"] == "unknown"
    assert enormous_payload["worker_protocol_version"] == "unknown"


@pytest.mark.parametrize(
    "unsafe_value",
    (
        pytest.param(
            "analysis.gwxproj",
            id="project-file",
            marks=pytest.mark.contract("TLD-005"),
        ),
        pytest.param(
            "experiment-2026",
            id="experiment-label",
            marks=pytest.mark.contract("TLD-006"),
        ),
        pytest.param(
            "recovery-20260906-01",
            id="recovery-label",
            marks=pytest.mark.contract("TLD-007"),
        ),
    ),
)
def test_diagnostics_reject_filename_like_build_ids_and_error_codes(
    monkeypatch: pytest.MonkeyPatch, unsafe_value: str
) -> None:
    """Build identity and error code fields cannot become a name leak channel."""
    from gwexpy_studio.runtime.diagnostics import (
        diagnostics_payload,
        format_diagnostics,
    )

    monkeypatch.setenv("GWEXPY_STUDIO_BUILD_ID", unsafe_value)
    monkeypatch.setenv("GWEXPY_STUDIO_SOURCE_COMMIT", unsafe_value)

    payload = diagnostics_payload(last_error_code=unsafe_value)
    text = format_diagnostics(last_error_code=unsafe_value)

    assert payload["build_id"] == "unknown"
    assert payload["source_commit"] == "unknown"
    assert payload["last_error_code"] == "unknown"
    assert unsafe_value not in text


@pytest.mark.contract("TLD-015")
def test_diagnostics_accepts_static_ui_error_codes_and_rejects_unknowns() -> None:
    """Studio-generated UI codes remain useful without opening a path channel."""
    from gwexpy_studio.runtime.diagnostics import diagnostics_payload

    static_ui_codes = (
        "bridge_busy",
        "bridge_unavailable",
        "command_pending",
        "export_unavailable",
        "invalid_drop",
        "invalid_response",
        "invalid_ui_state",
        "io_capability_unavailable",
        "runtime_dependency_missing",
        "probe_failed",
        "registry_missing",
        "unreviewed_registry_entry",
        "modal_active",
        "operation_unavailable",
        "recent_projects_unavailable",
        "sample_unavailable",
        "unsupported_source_format",
        "workspace_failed",
    )

    for code in static_ui_codes:
        assert diagnostics_payload(last_error_code=code)["last_error_code"] == code

    for unsafe_code in ("/private/experiment.csv", "unreviewed_ui_error"):
        assert diagnostics_payload(last_error_code=unsafe_code)["last_error_code"] == (
            "unknown"
        )


@pytest.mark.contract("TLD-008")
def test_format_diagnostics_filters_direct_malicious_values() -> None:
    """The text API has no generic payload escape hatch for private strings."""
    from gwexpy_studio.runtime.diagnostics import format_diagnostics

    text = format_diagnostics(
        project_schema_version="private-project.gwxproj",
        worker_protocol_version="recovery snapshot",
        io_capability_digest="private-data.csv",
        last_error_code="exception message with a path /tmp/secret",
    )

    assert "private-project.gwxproj" not in text
    assert "recovery snapshot" not in text
    assert "private-data.csv" not in text
    assert "/tmp/secret" not in text
    assert text.count("unknown") >= 4


@pytest.mark.contract("TLD-009")
def test_diagnostics_include_known_versions_and_safe_contract_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Known build and contract facts are available without importing PySide6."""
    from gwexpy_studio.runtime.diagnostics import diagnostics_payload

    monkeypatch.setenv("GWEXPY_STUDIO_BUILD_ID", "P-abcdef1-20260906-01")
    before = set(sys.modules)
    payload = diagnostics_payload(
        project_schema_version=3,
        worker_protocol_version=2,
        io_capability_digest="a" * 64,
        last_error_code="worker_timeout",
    )

    assert payload["build_id"] == "P-abcdef1-20260906-01"
    assert payload["project_schema_version"] == "3"
    assert payload["worker_protocol_version"] == "2"
    assert payload["io_capability_digest"] == "a" * 64
    assert payload["last_error_code"] == "worker_timeout"
    assert {"os", "platform", "python", "pyside6", "gwexpy", "gwpy"} <= set(payload)
    assert "PySide6" not in set(sys.modules) - before


@pytest.mark.contract("TLD-016")
def test_diagnostics_include_only_a_validated_effective_io_snapshot() -> None:
    """A worker snapshot is useful without becoming a path channel."""
    from gwexpy_studio.ops.io_capabilities import (
        CapabilityManifest,
        probe_effective_capabilities,
    )
    from gwexpy_studio.runtime.diagnostics import (
        diagnostics_payload,
        format_diagnostics,
    )

    snapshot = probe_effective_capabilities(CapabilityManifest(mode="developer"))
    payload = diagnostics_payload(io_capability_snapshot=snapshot.document())
    text = format_diagnostics(io_capability_snapshot=snapshot.document())

    assert json.loads(payload["io_capability_snapshot"]) == {
        "digest": snapshot.digest,
        "entry_count": 0,
        "mode": "developer",
        "policy_digest": None,
        "schema_version": 1,
        "status_counts": {
            "experimental": 0,
            "unavailable": 0,
            "verified": 0,
        },
        "unavailable_reason_counts": {},
    }
    assert "I/O capability snapshot: " + payload["io_capability_snapshot"] in text

    unsafe = diagnostics_payload(
        io_capability_snapshot={"private_path": "/home/alice/measurement.gwf"}
    )
    assert unsafe["io_capability_snapshot"] == "unknown"
    assert "/home/alice/measurement.gwf" not in format_diagnostics(
        io_capability_snapshot={"private_path": "/home/alice/measurement.gwf"}
    )


@pytest.mark.contract("TLD-017")
@pytest.mark.parametrize(
    ("entry", "secrets"),
    (
        pytest.param(
            {
                "datatype": "/home/alice/API_TOKEN_supersecret",
                "format": "csv",
                "direction": "read",
                "tier": "A",
                "status": "verified",
                "native_available": True,
                "auto_identify": True,
            },
            ("/home/alice/API_TOKEN_supersecret",),
            id="datatype-path",
        ),
        pytest.param(
            {
                "datatype": "TimeSeries",
                "format": "csv",
                "direction": "read",
                "tier": "A",
                "status": "verified",
                "native_available": False,
                "auto_identify": False,
            },
            (),
            id="inconsistent-native-fact",
        ),
        pytest.param(
            {
                "datatype": "TimeSeries",
                "format": _TEST_AWS_ACCESS_KEY,
                "direction": "read",
                "tier": "B",
                "status": "experimental",
                "native_available": True,
                "auto_identify": False,
                "caveat": _TEST_AWS_CAVEAT,
            },
            (
                _TEST_AWS_ACCESS_KEY,
                _TEST_AWS_CAVEAT,
            ),
            id="format-and-caveat-credential",
        ),
    ),
)
def test_diagnostics_rejects_malicious_or_inconsistent_capability_snapshots(
    entry: dict[str, object], secrets: tuple[str, ...]
) -> None:
    """A self-consistent digest cannot turn worker data into a diagnostics leak."""
    from gwexpy_studio.runtime.diagnostics import (
        diagnostics_payload,
        format_diagnostics,
    )

    unsigned = {
        "schema_version": 1,
        "mode": "frozen",
        "policy_digest": "a" * 64,
        "entries": [entry],
    }
    serialized = json.dumps(
        unsigned,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )
    snapshot = unsigned | {"digest": sha256(serialized.encode("utf-8")).hexdigest()}

    payload = diagnostics_payload(io_capability_snapshot=snapshot)
    text = format_diagnostics(io_capability_snapshot=snapshot)

    if entry["datatype"] != "TimeSeries" or entry["native_available"] is False:
        assert payload["io_capability_snapshot"] == "unknown"
    for secret in secrets:
        assert secret not in text


@pytest.mark.contract("TLD-010")
def test_runtime_logging_and_diagnostics_import_without_pyside6() -> None:
    """The runtime boundary remains importable on a non-Qt machine."""
    command = "\n".join(
        (
            "import sys",
            "import gwexpy_studio.runtime.diagnostics",
            "import gwexpy_studio.runtime.logging",
            "names = [",
            "    name",
            "    for name in sys.modules",
            "    if name == 'PySide6' or name.startswith('PySide6.')",
            "]",
            "print(','.join(sorted(names)))",
        )
    )
    environment = os.environ.copy()
    environment.pop("QT_QPA_PLATFORM", None)

    completed = subprocess.run(
        [sys.executable, "-c", command],
        check=False,
        capture_output=True,
        env=environment,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == ""


@pytest.mark.contract("TLD-011")
def test_diagnostics_treats_unreadable_package_metadata_as_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A metadata lookup failure cannot prevent a user from copying diagnostics."""
    import gwexpy_studio.runtime.diagnostics as diagnostics

    def unreadable_metadata(distribution: str) -> str:
        del distribution
        raise OSError("metadata unavailable")

    monkeypatch.setattr(diagnostics, "version", unreadable_metadata)

    payload = diagnostics.diagnostics_payload()

    assert payload["pyside6"] == "unknown"
    assert payload["gwexpy"] == "unknown"
    assert payload["gwpy"] == "unknown"
