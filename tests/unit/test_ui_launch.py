"""Headless contracts for the installed Studio launcher."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

SOURCE_ROOT = Path(__file__).resolve().parents[2] / "src"
_TRIAL_BUILD_ID = "P-abcdef0-20260907-r1-a1"
_TRIAL_SOURCE_SHA = "abcdef0123456789abcdef0123456789abcdef01"
_TRIAL_SOURCE_MANIFEST_SHA256 = "b" * 64
_TRIAL_VERSION = "0.1.0a1+trial.p.abcdef0.20260907.r1.a1"


def _write_trial_build_record(
    assets: Path,
    *,
    build_id: str = _TRIAL_BUILD_ID,
    source_sha: str = _TRIAL_SOURCE_SHA,
    source_manifest_sha256: str = _TRIAL_SOURCE_MANIFEST_SHA256,
    version: str = _TRIAL_VERSION,
) -> None:
    """Write the exact provenance record emitted into a trial wheel."""
    (assets / "trial-build.json").write_text(
        json.dumps(
            {
                "build_id": build_id,
                "schema": 1,
                "source_manifest_sha256": source_manifest_sha256,
                "source_sha": source_sha,
                "version": version,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )


def _write_trial_csv_policy(assets: Path) -> Path:
    """Write the reviewed Tier A CSV policy emitted into a trial wheel."""
    policy = assets / "io-capabilities.json"
    policy.write_text(
        """{
  "schema_version": 1,
  "entries": [
    {"datatype": "TimeSeries", "format": "csv", "direction": "read", "tier": "A"}
  ]
}
""",
        encoding="utf-8",
    )
    return policy


def _bind_installed_trial_version(
    monkeypatch: pytest.MonkeyPatch, app_module: object
) -> None:
    """Keep a package-resource test independent of this source checkout version."""
    monkeypatch.setattr(
        app_module,
        "_installed_package_version",
        lambda: _TRIAL_VERSION,
        raising=False,
    )


def test_parse_project_argument_accepts_one_canonical_project_path(
    tmp_path: Path,
) -> None:
    """The launcher accepts at most one `.gwxproj` path without reading it."""
    from gwexpy_studio.ui.launch import parse_project_argument

    target = tmp_path / "projects" / "trial.gwxproj"

    assert parse_project_argument([str(target.parent / "." / target.name)]) == (
        target.resolve(strict=False)
    )
    assert parse_project_argument([]) is None


@pytest.mark.parametrize(
    "arguments",
    [
        ["data.csv"],
        ["trial.gwxproj", "other.gwxproj"],
        ["--unexpected"],
    ],
)
def test_parse_project_argument_rejects_invalid_launcher_arguments(
    arguments: list[str],
) -> None:
    """CLI misuse fails before Qt or a worker process starts."""
    from gwexpy_studio.ui.launch import LauncherUsageError, parse_project_argument

    with pytest.raises(LauncherUsageError):
        parse_project_argument(arguments)


def test_importing_the_formal_launcher_bootstraps_matplotlib_before_ui_modules(
    tmp_path: Path,
) -> None:
    """The installed entry point sets Studio cache state before plot imports."""
    cache_home = tmp_path / "cache home"
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(SOURCE_ROOT)
    environment["XDG_CACHE_HOME"] = str(cache_home)
    environment.pop("MPLCONFIGDIR", None)
    probe = """
import os
import sys
import gwexpy_studio.ui.app

expected = os.path.join(os.environ['XDG_CACHE_HOME'], 'gwexpy-studio', 'matplotlib')
assert os.environ['MPLCONFIGDIR'] == expected
assert 'matplotlib' not in sys.modules
"""

    completed = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=SOURCE_ROOT.parent,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )

    assert completed.returncode == 0, completed.stderr


def test_main_freezes_then_routes_one_project_to_initial_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The formal entry point freezes before it can create Qt or a worker."""
    import gwexpy_studio.ui.app as app_module

    events: list[object] = []
    target = tmp_path / "project.gwxproj"

    class _Application:
        def exec(self) -> int:
            events.append("exec")
            return 17

    class _Window:
        def show(self) -> None:
            events.append("show")

        def initialize_workspace(self) -> None:
            events.append("recoveries")

        def start_initial_workspace(self, project_path: Path) -> None:
            events.append(("project", project_path))

    monkeypatch.setattr(
        app_module.multiprocessing,
        "freeze_support",
        lambda: events.append("freeze"),
    )
    monkeypatch.setattr(
        app_module,
        "create_app",
        lambda argv: (
            events.append(("create", tuple(argv))) or (_Application(), _Window())
        ),
    )

    assert app_module.main([str(target)]) == 17
    assert events == [
        "freeze",
        ("create", (sys.argv[0], str(target))),
        "show",
        ("project", target.resolve(strict=False)),
        "exec",
    ]


@pytest.mark.contract("TRT-ID-001")
def test_trial_launcher_binds_valid_embedded_identity_to_diagnostics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A valid wheel stamp overrides inherited identity only for the trial lifetime."""
    import gwexpy_studio.ui.app as app_module
    from gwexpy_studio.ops.io_capabilities import (
        CAPABILITY_ENVIRONMENT,
        KNOWN_IO_CLASSES,
        load_capability_manifest,
    )
    from gwexpy_studio.runtime.diagnostics import diagnostics_payload

    package = tmp_path / "gwexpy_studio"
    assets = package / "assets"
    assets.mkdir(parents=True)
    _write_trial_build_record(assets)
    embedded_policy = _write_trial_csv_policy(assets)
    monkeypatch.setattr(app_module.resources, "files", lambda _package: package)
    _bind_installed_trial_version(monkeypatch, app_module)
    monkeypatch.setenv(CAPABILITY_ENVIRONMENT, "/outside/unreviewed-policy.json")
    monkeypatch.setenv("GWEXPY_STUDIO_BUILD_ID", "P-deadbee-20260906-01")
    monkeypatch.setenv("GWEXPY_STUDIO_SOURCE_COMMIT", "d" * 40)

    with app_module._trial_capability_environment():
        assert os.environ[CAPABILITY_ENVIRONMENT] == str(embedded_policy)
        assert load_capability_manifest(io_classes=KNOWN_IO_CLASSES).mode == "frozen"
        payload = diagnostics_payload()
        assert payload["build_id"] == _TRIAL_BUILD_ID
        assert payload["source_commit"] == _TRIAL_SOURCE_SHA

    assert os.environ[CAPABILITY_ENVIRONMENT] == "/outside/unreviewed-policy.json"
    assert os.environ["GWEXPY_STUDIO_BUILD_ID"] == "P-deadbee-20260906-01"
    assert os.environ["GWEXPY_STUDIO_SOURCE_COMMIT"] == "d" * 40


@pytest.mark.contract("TRT-ID-002")
def test_trial_launcher_fails_closed_for_mismatched_embedded_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stamp that disagrees with installed metadata cannot enable trial I/O."""
    import gwexpy_studio.ui.app as app_module
    from gwexpy_studio.ops.io_capabilities import (
        CAPABILITY_ENVIRONMENT,
        KNOWN_IO_CLASSES,
        load_capability_manifest,
    )
    from gwexpy_studio.runtime.diagnostics import diagnostics_payload

    package = tmp_path / "gwexpy_studio"
    assets = package / "assets"
    assets.mkdir(parents=True)
    _write_trial_build_record(assets)
    _write_trial_csv_policy(assets)
    monkeypatch.setattr(app_module.resources, "files", lambda _package: package)
    monkeypatch.setattr(
        app_module,
        "_installed_package_version",
        lambda: "0.1.0a1",
        raising=False,
    )
    monkeypatch.setenv(CAPABILITY_ENVIRONMENT, "/outside/unreviewed-policy.json")
    monkeypatch.setenv("GWEXPY_STUDIO_BUILD_ID", "P-deadbee-20260906-01")
    monkeypatch.setenv("GWEXPY_STUDIO_SOURCE_COMMIT", "d" * 40)

    with app_module._trial_capability_environment():
        assert load_capability_manifest(io_classes=KNOWN_IO_CLASSES).mode == "invalid"
        payload = diagnostics_payload()
        assert payload["build_id"] == "unknown"
        assert payload["source_commit"] == "unknown"

    assert os.environ[CAPABILITY_ENVIRONMENT] == "/outside/unreviewed-policy.json"
    assert os.environ["GWEXPY_STUDIO_BUILD_ID"] == "P-deadbee-20260906-01"
    assert os.environ["GWEXPY_STUDIO_SOURCE_COMMIT"] == "d" * 40


@pytest.mark.contract("TRT-ID-003")
def test_trial_launcher_rejects_an_identity_with_an_impossible_build_date(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A syntactically shaped but impossible CI date cannot enable a trial wheel."""
    import gwexpy_studio.ui.app as app_module
    from gwexpy_studio.ops.io_capabilities import (
        CAPABILITY_ENVIRONMENT,
        KNOWN_IO_CLASSES,
        load_capability_manifest,
    )

    version = "0.1.0a1+trial.p.abcdef0.20260230.r1.a1"
    package = tmp_path / "gwexpy_studio"
    assets = package / "assets"
    assets.mkdir(parents=True)
    _write_trial_build_record(
        assets,
        build_id="P-abcdef0-20260230-r1-a1",
        version=version,
    )
    _write_trial_csv_policy(assets)
    monkeypatch.setattr(app_module.resources, "files", lambda _package: package)
    monkeypatch.setattr(
        app_module,
        "_installed_package_version",
        lambda: version,
        raising=False,
    )
    monkeypatch.setenv(CAPABILITY_ENVIRONMENT, "/outside/unreviewed-policy.json")

    with app_module._trial_capability_environment():
        assert load_capability_manifest(io_classes=KNOWN_IO_CLASSES).mode == "invalid"

    assert os.environ[CAPABILITY_ENVIRONMENT] == "/outside/unreviewed-policy.json"


@pytest.mark.contract("TRT-ID-004")
def test_source_launcher_preserves_existing_identity_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A source checkout without a generated stamp retains development settings."""
    import gwexpy_studio.ui.app as app_module
    from gwexpy_studio.ops.io_capabilities import CAPABILITY_ENVIRONMENT

    assert not app_module._trial_resource(*app_module._TRIAL_BUILD_RESOURCE).is_file()
    monkeypatch.setenv(CAPABILITY_ENVIRONMENT, "/developer/policy.json")
    monkeypatch.setenv("GWEXPY_STUDIO_BUILD_ID", "P-deadbee-20260906-01")
    monkeypatch.setenv("GWEXPY_STUDIO_SOURCE_COMMIT", "d" * 40)

    with app_module._trial_capability_environment():
        assert os.environ[CAPABILITY_ENVIRONMENT] == "/developer/policy.json"
        assert os.environ["GWEXPY_STUDIO_BUILD_ID"] == "P-deadbee-20260906-01"
        assert os.environ["GWEXPY_STUDIO_SOURCE_COMMIT"] == "d" * 40


@pytest.mark.contract("TRT-ID-005")
def test_trial_version_without_embedded_identity_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A damaged trial wheel cannot become an unrestricted source launch."""
    import gwexpy_studio.ui.app as app_module
    from gwexpy_studio.ops.io_capabilities import (
        CAPABILITY_ENVIRONMENT,
        KNOWN_IO_CLASSES,
        load_capability_manifest,
    )

    package = tmp_path / "gwexpy_studio"
    package.mkdir()
    monkeypatch.setattr(app_module.resources, "files", lambda _package: package)
    _bind_installed_trial_version(monkeypatch, app_module)
    monkeypatch.setenv(CAPABILITY_ENVIRONMENT, "/outside/unreviewed-policy.json")
    monkeypatch.setenv("GWEXPY_STUDIO_BUILD_ID", "P-deadbee-20260906-01")
    monkeypatch.setenv("GWEXPY_STUDIO_SOURCE_COMMIT", "d" * 40)

    with app_module._trial_capability_environment():
        assert os.environ[CAPABILITY_ENVIRONMENT] != "/outside/unreviewed-policy.json"
        assert load_capability_manifest(io_classes=KNOWN_IO_CLASSES).mode == "invalid"
        assert "GWEXPY_STUDIO_BUILD_ID" not in os.environ
        assert "GWEXPY_STUDIO_SOURCE_COMMIT" not in os.environ

    assert os.environ[CAPABILITY_ENVIRONMENT] == "/outside/unreviewed-policy.json"
    assert os.environ["GWEXPY_STUDIO_BUILD_ID"] == "P-deadbee-20260906-01"
    assert os.environ["GWEXPY_STUDIO_SOURCE_COMMIT"] == "d" * 40


@pytest.mark.contract("TRT-IO-001")
def test_trial_launcher_uses_its_embedded_policy_not_an_external_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The installed launcher binds worker startup to the wheel's CSV policy."""
    import gwexpy_studio.ui.app as app_module
    from gwexpy_studio.ops.io_capabilities import (
        CAPABILITY_ENVIRONMENT,
        KNOWN_IO_CLASSES,
        load_capability_manifest,
    )

    package = tmp_path / "gwexpy_studio"
    assets = package / "assets"
    assets.mkdir(parents=True)
    _write_trial_build_record(assets)
    embedded_policy = _write_trial_csv_policy(assets)
    monkeypatch.setattr(app_module.resources, "files", lambda _package: package)
    _bind_installed_trial_version(monkeypatch, app_module)
    monkeypatch.setenv(CAPABILITY_ENVIRONMENT, "/outside/unreviewed-policy.json")

    with app_module._trial_capability_environment():
        active = load_capability_manifest(io_classes=KNOWN_IO_CLASSES)
        assert active.mode == "frozen"
        assert active.capability("TimeSeries", "csv", "read").tier == "A"
        assert os.environ[CAPABILITY_ENVIRONMENT] == str(embedded_policy)

    assert os.environ[CAPABILITY_ENVIRONMENT] == "/outside/unreviewed-policy.json"


@pytest.mark.contract("TRT-IO-005")
def test_main_binds_trial_policy_before_workspace_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The console launcher has the embedded policy set before it can spawn."""
    import gwexpy_studio.ui.app as app_module
    from gwexpy_studio.ops.io_capabilities import (
        CAPABILITY_ENVIRONMENT,
        KNOWN_IO_CLASSES,
        load_capability_manifest,
    )

    package = tmp_path / "gwexpy_studio"
    assets = package / "assets"
    assets.mkdir(parents=True)
    _write_trial_build_record(assets)
    _write_trial_csv_policy(assets)
    monkeypatch.setattr(app_module.resources, "files", lambda _package: package)
    _bind_installed_trial_version(monkeypatch, app_module)
    monkeypatch.setenv(CAPABILITY_ENVIRONMENT, "/outside/unreviewed-policy.json")
    observed: list[object] = []

    class _Application:
        def exec(self) -> int:
            observed.append(load_capability_manifest(io_classes=KNOWN_IO_CLASSES))
            return 0

    class _Window:
        def show(self) -> None:
            observed.append("show")

        def initialize_workspace(self) -> None:
            observed.append(load_capability_manifest(io_classes=KNOWN_IO_CLASSES))

    monkeypatch.setattr(
        app_module,
        "create_app",
        lambda _argv: (_Application(), _Window()),
    )

    assert app_module.main([]) == 0
    assert observed[0] == "show"
    for manifest in observed[1:]:
        assert manifest.mode == "frozen"
        assert manifest.capability("TimeSeries", "csv", "read").tier == "A"
    assert os.environ[CAPABILITY_ENVIRONMENT] == "/outside/unreviewed-policy.json"


@pytest.mark.contract("TRT-IO-002")
def test_trial_launcher_fails_closed_when_its_embedded_policy_is_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A trial identity without its policy cannot inherit an external override."""
    import gwexpy_studio.ui.app as app_module
    from gwexpy_studio.ops.io_capabilities import (
        CAPABILITY_ENVIRONMENT,
        KNOWN_IO_CLASSES,
        load_capability_manifest,
    )

    package = tmp_path / "gwexpy_studio"
    assets = package / "assets"
    assets.mkdir(parents=True)
    _write_trial_build_record(assets)
    monkeypatch.setattr(app_module.resources, "files", lambda _package: package)
    _bind_installed_trial_version(monkeypatch, app_module)
    monkeypatch.setenv(CAPABILITY_ENVIRONMENT, "/outside/unreviewed-policy.json")

    with app_module._trial_capability_environment():
        assert os.environ[CAPABILITY_ENVIRONMENT] != "/outside/unreviewed-policy.json"
        assert load_capability_manifest(io_classes=KNOWN_IO_CLASSES).mode == "invalid"


@pytest.mark.contract("TRT-IO-003")
def test_trial_launcher_fails_closed_when_its_embedded_policy_is_invalid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A malformed package policy never falls back to an inherited environment."""
    import gwexpy_studio.ui.app as app_module
    from gwexpy_studio.ops.io_capabilities import (
        CAPABILITY_ENVIRONMENT,
        KNOWN_IO_CLASSES,
        load_capability_manifest,
    )

    package = tmp_path / "gwexpy_studio"
    assets = package / "assets"
    assets.mkdir(parents=True)
    _write_trial_build_record(assets)
    (assets / "io-capabilities.json").write_text("not json\n", encoding="utf-8")
    monkeypatch.setattr(app_module.resources, "files", lambda _package: package)
    _bind_installed_trial_version(monkeypatch, app_module)
    monkeypatch.setenv(CAPABILITY_ENVIRONMENT, "/outside/unreviewed-policy.json")

    with app_module._trial_capability_environment():
        assert os.environ[CAPABILITY_ENVIRONMENT] != "/outside/unreviewed-policy.json"
        assert load_capability_manifest(io_classes=KNOWN_IO_CLASSES).mode == "invalid"


@pytest.mark.contract("TRT-LOG-002")
def test_main_installs_and_restores_its_logging_lifecycle_around_qt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A valid launch owns logging only for the lifetime of its Qt app."""
    import gwexpy_studio.ui.app as app_module

    events: list[object] = []
    logger = object()
    created_log_handler = object()
    qt_handler = object()
    previous_qt_handler = object()
    handler_snapshots = iter((frozenset(), frozenset({created_log_handler})))

    class _Hooks:
        def restore(self) -> None:
            events.append("restore-hooks")

    class _Application:
        def exec(self) -> int:
            events.append("exec")
            return 23

    class _Window:
        def show(self) -> None:
            events.append("show")

        def initialize_workspace(self) -> None:
            events.append("initialize")

    monkeypatch.setattr(
        app_module.multiprocessing,
        "freeze_support",
        lambda: events.append("freeze"),
    )
    monkeypatch.setattr(
        app_module,
        "configure_studio_logging",
        lambda: events.append("configure-logger") or logger,
    )
    monkeypatch.setattr(
        app_module,
        "owned_studio_log_handlers",
        lambda: next(handler_snapshots),
        raising=False,
    )
    monkeypatch.setattr(
        app_module,
        "active_exception_hook_registration",
        lambda: None,
        raising=False,
    )
    monkeypatch.setattr(
        app_module,
        "install_exception_hooks",
        lambda *, logger: events.append(("install-hooks", logger)) or _Hooks(),
    )
    monkeypatch.setattr(
        app_module,
        "make_qt_message_handler",
        lambda configured_logger: events.append(("make-qt-handler", configured_logger))
        or qt_handler,
    )
    monkeypatch.setattr(
        app_module,
        "qInstallMessageHandler",
        lambda handler: events.append(("install-qt-handler", handler))
        or previous_qt_handler,
    )
    monkeypatch.setattr(
        app_module,
        "shutdown_studio_logging",
        lambda *, owned_handlers: events.append(
            ("shutdown-logger", owned_handlers)
        ),
    )
    monkeypatch.setattr(
        app_module,
        "create_app",
        lambda _argv: events.append("create-app") or (_Application(), _Window()),
    )

    assert app_module.main([]) == 23
    assert events == [
        "freeze",
        "configure-logger",
        ("install-hooks", logger),
        ("make-qt-handler", logger),
        ("install-qt-handler", qt_handler),
        "create-app",
        "show",
        "initialize",
        "exec",
        ("install-qt-handler", None),
        ("install-qt-handler", previous_qt_handler),
        "restore-hooks",
        ("shutdown-logger", frozenset({created_log_handler})),
    ]


@pytest.mark.contract("TRT-LOG-003")
def test_main_continues_without_logging_when_setup_fails(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A broken private log location never prevents a valid GUI launch."""
    import gwexpy_studio.ui.app as app_module

    events: list[str] = []

    class _Application:
        def exec(self) -> int:
            events.append("exec")
            return 29

    class _Window:
        def show(self) -> None:
            events.append("show")

        def initialize_workspace(self) -> None:
            events.append("initialize")

    def fail_logging() -> object:
        events.append("configure-logger")
        raise OSError("/private/experiment/secret-path")

    def unexpected(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("logging fallback must not install another component")

    monkeypatch.setattr(app_module, "configure_studio_logging", fail_logging)
    monkeypatch.setattr(app_module, "install_exception_hooks", unexpected)
    monkeypatch.setattr(app_module, "qInstallMessageHandler", lambda _handler: None)
    monkeypatch.setattr(app_module, "shutdown_studio_logging", unexpected)
    monkeypatch.setattr(
        app_module,
        "create_app",
        lambda _argv: events.append("create-app") or (_Application(), _Window()),
    )

    assert app_module.main([]) == 29
    assert events == ["configure-logger", "create-app", "show", "initialize", "exec"]
    assert capsys.readouterr().err == ""


@pytest.mark.contract("TRT-LOG-004")
def test_main_discards_qt_messages_when_log_setup_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A log-path failure cannot return Qt's private messages to stderr."""
    import gwexpy_studio.ui.app as app_module

    logger_setup_calls: list[str] = []
    installed_handlers: list[object | None] = []
    previous_handler = object()
    state: dict[str, object | None] = {"handler": previous_handler}

    class _Application:
        def exec(self) -> int:
            return 37

    class _Window:
        def show(self) -> None:
            return None

        def initialize_workspace(self) -> None:
            return None

    def fail_logging() -> object:
        logger_setup_calls.append("configure")
        raise OSError("private-log-path")

    def install_qt_handler(handler: object | None) -> object | None:
        installed_handlers.append(handler)
        prior = state["handler"]
        state["handler"] = handler
        return prior

    def unexpected(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("failed file logging must not install file-backed hooks")

    monkeypatch.setattr(app_module, "configure_studio_logging", fail_logging)
    monkeypatch.setattr(app_module, "install_exception_hooks", unexpected)
    monkeypatch.setattr(app_module, "make_qt_message_handler", unexpected)
    monkeypatch.setattr(app_module, "shutdown_studio_logging", unexpected)
    monkeypatch.setattr(app_module, "qInstallMessageHandler", install_qt_handler)
    monkeypatch.setattr(
        app_module,
        "create_app",
        lambda _argv: (_Application(), _Window()),
    )

    assert app_module.main([]) == 37
    assert logger_setup_calls == ["configure"]
    assert installed_handlers[0] is app_module._discard_qt_message
    assert installed_handlers[1:] == [None, previous_handler]
    assert state["handler"] is previous_handler
    app_module._discard_qt_message(object(), object(), "private-qt-message")


@pytest.mark.contract("TRT-LOG-005")
def test_failed_logging_configuration_discards_real_qt_warning_from_stderr(
    tmp_path: Path,
) -> None:
    """The real Qt handler has no path-text escape when logging is unavailable."""
    state_home = tmp_path / "state"
    unrelated = tmp_path / "unrelated"
    state_home.mkdir()
    unrelated.mkdir()
    (state_home / "gwexpy-studio").symlink_to(unrelated, target_is_directory=True)
    private_token = "private-qt-warning-token"
    environment = os.environ | {
        "PYTHONPATH": str(SOURCE_ROOT),
        "QT_QPA_PLATFORM": "offscreen",
        "XDG_STATE_HOME": str(state_home),
    }
    command = "\n".join(
        (
            "from PySide6.QtCore import qWarning",
            "from gwexpy_studio.ui import app",
            "lifecycle = app._configure_launch_logging()",
            f"qWarning({private_token!r})",
            "lifecycle.close()",
        )
    )

    completed = subprocess.run(
        [sys.executable, "-c", command],
        cwd=SOURCE_ROOT.parent,
        capture_output=True,
        check=False,
        env=environment,
        text=True,
        timeout=15,
    )

    assert completed.returncode == 0, completed.stderr
    assert private_token not in completed.stderr


@pytest.mark.contract("TRT-LOG-006")
def test_main_leaves_preexisting_studio_logging_resources_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A nested launcher cannot close the logger or hooks it did not create."""
    import gwexpy_studio.ui.app as app_module

    existing_handler = object()
    logger = object()
    shutdown_calls: list[object] = []

    class _ExistingHooks:
        restore_calls = 0

        def restore(self) -> None:
            self.restore_calls += 1

    existing_hooks = _ExistingHooks()

    class _Application:
        def exec(self) -> int:
            return 41

    class _Window:
        def show(self) -> None:
            return None

        def initialize_workspace(self) -> None:
            return None

    monkeypatch.setattr(app_module, "configure_studio_logging", lambda: logger)
    monkeypatch.setattr(
        app_module,
        "owned_studio_log_handlers",
        lambda: frozenset({existing_handler}),
        raising=False,
    )
    monkeypatch.setattr(
        app_module,
        "active_exception_hook_registration",
        lambda: existing_hooks,
        raising=False,
    )
    monkeypatch.setattr(
        app_module,
        "install_exception_hooks",
        lambda *, logger: existing_hooks,
    )
    monkeypatch.setattr(
        app_module,
        "make_qt_message_handler",
        lambda _logger: object(),
    )
    monkeypatch.setattr(app_module, "qInstallMessageHandler", lambda _handler: None)
    monkeypatch.setattr(
        app_module,
        "shutdown_studio_logging",
        lambda **_kwargs: shutdown_calls.append("shutdown"),
    )
    monkeypatch.setattr(
        app_module,
        "create_app",
        lambda _argv: (_Application(), _Window()),
    )

    assert app_module.main([]) == 41
    assert existing_hooks.restore_calls == 0
    assert shutdown_calls == []


@pytest.mark.contract("TRT-LOG-007")
def test_main_does_not_clobber_a_qt_handler_replaced_while_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cleanup restores Studio's predecessor only while Studio still owns Qt."""
    import gwexpy_studio.ui.app as app_module

    logger = object()
    studio_qt_handler = object()
    original_qt_handler = object()
    external_qt_handler = object()
    state: dict[str, object | None] = {"handler": original_qt_handler}

    class _Hooks:
        def restore(self) -> None:
            return None

    class _Application:
        def exec(self) -> int:
            state["handler"] = external_qt_handler
            return 31

    class _Window:
        def show(self) -> None:
            return None

        def initialize_workspace(self) -> None:
            return None

    def install_qt_handler(handler: object | None) -> object | None:
        previous = state["handler"]
        state["handler"] = handler
        return previous

    monkeypatch.setattr(app_module, "configure_studio_logging", lambda: logger)
    monkeypatch.setattr(
        app_module,
        "owned_studio_log_handlers",
        lambda: frozenset(),
        raising=False,
    )
    monkeypatch.setattr(
        app_module,
        "active_exception_hook_registration",
        lambda: None,
        raising=False,
    )
    monkeypatch.setattr(
        app_module, "install_exception_hooks", lambda *, logger: _Hooks()
    )
    monkeypatch.setattr(
        app_module, "make_qt_message_handler", lambda _logger: studio_qt_handler
    )
    monkeypatch.setattr(app_module, "qInstallMessageHandler", install_qt_handler)
    monkeypatch.setattr(app_module, "shutdown_studio_logging", lambda: None)
    monkeypatch.setattr(
        app_module,
        "create_app",
        lambda _argv: (_Application(), _Window()),
    )

    assert app_module.main([]) == 31
    assert state["handler"] is external_qt_handler


def test_main_rejects_invalid_arguments_before_qt_creation(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A CLI misuse cannot create a QApplication or worker process."""
    import gwexpy_studio.ui.app as app_module

    called = False
    logging_called = False

    def unexpected_create(_argv: object) -> object:
        nonlocal called
        called = True
        raise AssertionError("Qt must not be created for invalid launcher input")

    monkeypatch.setattr(app_module, "create_app", unexpected_create)

    def unexpected_logging() -> object:
        nonlocal logging_called
        logging_called = True
        raise AssertionError("logging must not be configured for invalid input")

    monkeypatch.setattr(app_module, "configure_studio_logging", unexpected_logging)

    assert app_module.main(["not-a-project.csv"]) == 2
    assert not called
    assert not logging_called
    assert "expected a .gwxproj project path" in capsys.readouterr().err


def test_wheel_declares_the_gui_launcher(tmp_path: Path) -> None:
    """Installing the public wheel exposes the formal GUI entry point."""
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            "--no-deps",
            "--no-build-isolation",
            "--wheel-dir",
            str(wheelhouse),
            str(SOURCE_ROOT.parent),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr
    wheel = next(wheelhouse.glob("*.whl"))
    with zipfile.ZipFile(wheel) as archive:
        entry_points = archive.read(
            next(
                name for name in archive.namelist() if name.endswith("entry_points.txt")
            )
        ).decode("utf-8")

    assert "[gui_scripts]" in entry_points
    assert "gwexpy-studio = gwexpy_studio.ui.app:main" in entry_points
