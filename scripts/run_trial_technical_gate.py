"""Run a bounded offscreen technical gate against an installed trial wheel."""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import uuid
from collections.abc import Callable, Mapping, Sequence
from multiprocessing import shared_memory
from pathlib import Path
from typing import Any

sys.dont_write_bytecode = True


class GateError(RuntimeError):
    """Raised when the installed-wheel technical gate cannot be trusted."""


class _ScheduledMessageClick:
    """Retain one bounded, Qt-owned message-box interaction."""

    def __init__(self, *, poll_timer: Any, deadline_timer: Any) -> None:
        self._poll_timer = poll_timer
        self._deadline_timer = deadline_timer
        self.error: BaseException | None = None
        self.handled = False

    def stop(self) -> None:
        """Stop both owner-bound timers once this interaction is settled."""
        self._poll_timer.stop()
        self._deadline_timer.stop()

    def raise_if_failed(self) -> None:
        """Re-raise a callback failure after its nested Qt modal loop returns."""
        if self.error is not None:
            raise GateError("technical-gate message interaction failed") from self.error


_CHECK_NAMES = (
    "launcher_import",
    "welcome",
    "try_sample",
    "crop",
    "asd",
    "save_project",
    "project_reopen",
    "recovery",
    "worker_exit",
    "shared_memory_cleanup",
)
_MACHINES = frozenset({"x86_64", "aarch64", "arm64"})
_PYTHON_VERSION = re.compile(r"3\.12\.[0-9]+")
_SOURCE_SHA = re.compile(r"[0-9a-f]{40}")
_BUILD_ID = re.compile(r"P-[0-9a-f]{7}-[0-9]{8}-r[1-9][0-9]*-a[1-9][0-9]*")
_TRIAL_VERSION = re.compile(
    r"0\.1\.0a1\+trial\.p\.g[0-9a-f]{7}\.[0-9]{8}\.r[1-9][0-9]*\.a[1-9][0-9]*"
)
_GATE_STAGE_FILENAME = "technical-gate-stage.json"
_GATE_STAGE_TOKEN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_SEALED_GATE_BOOTSTRAP = (
    "import os as _os\n"
    "import sys as _sys\n"
    "_gate_fd = int(_sys.argv[1])\n"
    "_os.lseek(_gate_fd, 0, _os.SEEK_SET)\n"
    "with _os.fdopen(_gate_fd, 'rb', closefd=False) as _gate_source:\n"
    "    _gate_code = _gate_source.read()\n"
    "_sys.argv = ['<sealed-trial-gate>', *_sys.argv[2:]]\n"
    "_gate_globals = {'__name__': '__main__', '__file__': '<sealed-trial-gate>'}\n"
    "exec(compile(_gate_code, '<sealed-trial-gate>', 'exec'), _gate_globals)\n"
)


def _qapplication_arguments() -> list[str]:
    """Return the concrete argv container accepted by current PySide6 builds."""
    return [sys.argv[0]]


def _record_gate_stage(work_root: Path, phase: str, stage: str) -> None:
    """Persist one bounded, path-free phase marker for failed CI diagnosis."""
    if (
        phase not in {"producer", "consumer"}
        or _GATE_STAGE_TOKEN.fullmatch(stage) is None
    ):
        raise GateError("technical-gate diagnostic stage is invalid")
    path = work_root / _GATE_STAGE_FILENAME
    if path.is_symlink():
        raise GateError("technical-gate diagnostic stage path is unsafe")
    try:
        path.write_bytes(
            json.dumps(
                {"phase": phase, "stage": stage},
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\n"
        )
    except OSError as exc:
        raise GateError(
            "technical-gate diagnostic stage could not be recorded"
        ) from exc


def read_gate_stage(work_root: Path) -> str:
    """Read one strict phase marker without returning host-specific content."""
    path = work_root / _GATE_STAGE_FILENAME
    try:
        if path.is_symlink() or not path.is_file():
            raise GateError("technical-gate diagnostic stage is unavailable")
        content = path.read_bytes()
        document = json.loads(content.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise GateError("technical-gate diagnostic stage is invalid") from exc
    if (
        not isinstance(document, dict)
        or set(document) != {"phase", "stage"}
        or document["phase"] not in {"producer", "consumer"}
        or not isinstance(document["stage"], str)
        or _GATE_STAGE_TOKEN.fullmatch(document["stage"]) is None
    ):
        raise GateError("technical-gate diagnostic stage is invalid")
    return f"{document['phase']}/{document['stage']}"


def verify_installed_module_path(
    *,
    module_path: Path,
    site_roots: Sequence[Path],
    checkout_root: Path,
) -> None:
    """Require an import to resolve from the fresh venv, never P or an overlay."""
    try:
        module = module_path.resolve(strict=True)
        checkout = checkout_root.resolve(strict=True)
    except OSError as exc:
        raise GateError("installed package path cannot be resolved") from exc
    if _is_within(module, checkout):
        raise GateError("Studio imported from the checkout")
    roots: list[Path] = []
    for root in site_roots:
        try:
            resolved_root = root.resolve(strict=True)
        except FileNotFoundError:
            # Ubuntu's system-Python venv reports optional dist-packages paths
            # which do not necessarily exist in the fresh environment.
            continue
        except OSError as exc:
            raise GateError("installed site-packages path cannot be resolved") from exc
        if not resolved_root.is_dir():
            raise GateError("installed site-packages path is not a directory")
        roots.append(resolved_root)
    if not roots or not any(_is_within(module, root) for root in roots):
        raise GateError("Studio did not import from installed site-packages")


def gate_environment(
    root: Path,
    inherited: Mapping[str, str],
    shm_prefix: str,
    *,
    native_qt: bool = False,
) -> dict[str, str]:
    """Build the isolated environment used by the external offscreen process."""
    safe_characters = (
        "-_.0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
    )
    if not shm_prefix or any(
        character not in safe_characters for character in shm_prefix
    ):
        raise GateError("technical-gate shared-memory prefix is unsafe")
    environment = dict(inherited)
    for name in tuple(environment):
        if name in {
            "GWEXPY_STUDIO_IO_CAPABILITIES",
            "PYTHONHOME",
            "PYTHONPATH",
            *(("QT_QPA_PLATFORM",) if native_qt else ()),
        } or name.startswith("PIP_"):
            environment.pop(name, None)
    environment.update(
        {
            "HOME": str(root / "home"),
            "MPLBACKEND": "Agg",
            "MPLCONFIGDIR": str(root / "mpl"),
            "XDG_CACHE_HOME": str(root / "cache"),
            "XDG_CONFIG_HOME": str(root / "config"),
            "XDG_DATA_HOME": str(root / "data"),
            "XDG_STATE_HOME": str(root / "state"),
            "GWEXPY_STUDIO_SHM_PREFIX": shm_prefix,
        }
    )
    if not native_qt:
        environment["QT_QPA_PLATFORM"] = "offscreen"
    return environment


def shared_memory_cleanup_probe(prefix: str) -> bool:
    """Prove portable unlink semantics by rejecting a post-unlink reattach."""
    safe_characters = (
        "-_.0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
    )
    if not prefix or any(character not in safe_characters for character in prefix):
        raise GateError("technical-gate shared-memory prefix is unsafe")
    name = f"{prefix}cleanup-probe-{uuid.uuid4().hex}"
    block: shared_memory.SharedMemory | None = None
    try:
        block = shared_memory.SharedMemory(name=name, create=True, size=1)
        block.unlink()
        block.close()
        block = None
        try:
            unexpected = shared_memory.SharedMemory(name=name)
        except FileNotFoundError:
            return True
        unexpected.close()
        return False
    except (FileExistsError, OSError) as exc:
        raise GateError("technical-gate shared-memory probe failed") from exc
    finally:
        if block is not None:
            try:
                block.close()
                block.unlink()
            except FileNotFoundError:
                pass


def gate_result_json(
    checks: Mapping[str, object], *, installed: Mapping[str, object]
) -> bytes:
    """Serialize only named boolean outcomes, never host paths or exception text."""
    if set(checks) != set(_CHECK_NAMES) or any(
        type(checks[name]) is not bool for name in _CHECK_NAMES
    ):
        raise GateError("technical-gate checks are invalid")
    identity = _gate_identity(installed)
    successful = all(checks[name] for name in _CHECK_NAMES)
    return (
        json.dumps(
            {
                "architecture": identity["architecture"],
                "checks": {name: checks[name] for name in sorted(_CHECK_NAMES)},
                "installed": {
                    name: identity[name]
                    for name in ("build_id", "source_sha", "version")
                },
                "python_version": identity["python_version"],
                "schema": 2,
                "status": "passed" if successful else "failed",
            },
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )


def read_gate_result(content: bytes) -> dict[str, object]:
    """Validate the bounded record consumed by resolution capture."""
    try:
        document = json.loads(content.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise GateError("technical-gate result is not valid JSON") from exc
    if type(document) is not dict or set(document) != {
        "architecture",
        "checks",
        "installed",
        "python_version",
        "schema",
        "status",
    }:
        raise GateError("technical-gate result schema is invalid")
    checks = document["checks"]
    if not isinstance(checks, dict) or set(checks) != set(_CHECK_NAMES):
        raise GateError("technical-gate result checks are invalid")
    if any(type(checks[name]) is not bool for name in _CHECK_NAMES):
        raise GateError("technical-gate result checks are invalid")
    if document["schema"] != 2:
        raise GateError("technical-gate result schema is unsupported")
    expected_status = "passed" if all(checks.values()) else "failed"
    if document["status"] != expected_status:
        raise GateError("technical-gate result status is inconsistent")
    return {
        "architecture": _gate_identity(document)["architecture"],
        "checks": {name: checks[name] for name in sorted(_CHECK_NAMES)},
        "installed": {
            name: _gate_identity(document)[name]
            for name in ("build_id", "source_sha", "version")
        },
        "python_version": _gate_identity(document)["python_version"],
        "schema": 2,
        "status": expected_status,
    }


def _gate_identity(value: Mapping[str, object]) -> dict[str, str]:
    """Validate path-free installed-wheel provenance captured by the gate."""
    installed = value.get("installed", value)
    if not isinstance(installed, Mapping):
        raise GateError("technical-gate installed identity is invalid")
    fields = {
        "architecture": value.get("architecture"),
        "python_version": value.get("python_version"),
        "build_id": installed.get("build_id"),
        "source_sha": installed.get("source_sha"),
        "version": installed.get("version"),
    }
    if any(not isinstance(item, str) for item in fields.values()):
        raise GateError("technical-gate installed identity is invalid")
    identity = {name: item for name, item in fields.items() if isinstance(item, str)}
    if (
        identity["architecture"] not in _MACHINES
        or _PYTHON_VERSION.fullmatch(identity["python_version"]) is None
        or _BUILD_ID.fullmatch(identity["build_id"]) is None
        or _SOURCE_SHA.fullmatch(identity["source_sha"]) is None
        or _TRIAL_VERSION.fullmatch(identity["version"]) is None
    ):
        raise GateError("technical-gate installed identity is invalid")
    return identity


def _installed_identity() -> dict[str, str]:
    """Read only the identity embedded in the installed package resources."""
    from importlib import resources

    try:
        document = json.loads(
            resources.files("gwexpy_studio")
            .joinpath("assets", "trial-build.json")
            .read_text(encoding="utf-8")
        )
    except (ModuleNotFoundError, OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise GateError("installed trial identity is unavailable") from exc
    if not isinstance(document, dict):
        raise GateError("installed trial identity is invalid")
    return _gate_identity(
        {
            "architecture": platform.machine().lower(),
            "python_version": ".".join(map(str, sys.version_info[:3])),
            "installed": document,
        }
    )


def technical_gate_command(
    *,
    python: Path,
    gate_fd: int,
    checkout_root: Path,
    work_root: Path,
    result_path: Path,
) -> tuple[str, ...]:
    """Build an isolated invocation that reads only a retained sealed FD.

    Capture writes the source bytes from public commit P, verifies them, then
    unlinks the temporary name before passing this descriptor through every
    process boundary.  The interpreter bootstrap therefore cannot re-open a
    mutable checkout or workspace path after source verification.
    """
    gate_fd = _validated_gate_fd(gate_fd)
    return (
        str(python),
        "-I",
        "-c",
        _SEALED_GATE_BOOTSTRAP,
        str(gate_fd),
        "--gate-fd",
        str(gate_fd),
        "--checkout",
        str(checkout_root),
        "--work-root",
        str(work_root),
        "--result",
        str(result_path),
    )


def _phase_command(
    *,
    phase: str,
    python: Path,
    gate_fd: int,
    checkout: Path,
    work_root: Path,
    project: Path | None = None,
) -> tuple[str, ...]:
    """Build one fresh installed-wheel producer or consumer invocation."""
    if phase not in {"producer", "consumer"}:
        raise GateError("technical-gate phase is invalid")
    if (phase == "consumer") != (project is not None):
        raise GateError("technical-gate phase project is invalid")
    gate_fd = _validated_gate_fd(gate_fd)
    command = (
        str(python),
        "-I",
        "-c",
        _SEALED_GATE_BOOTSTRAP,
        str(gate_fd),
        "--phase",
        phase,
        "--gate-fd",
        str(gate_fd),
        "--checkout",
        str(checkout),
        "--work-root",
        str(work_root),
    )
    return command if project is None else (*command, "--project", str(project))


def run_external_recovery_gate(
    *,
    checks: dict[str, bool],
    checkout: Path,
    gate_fd: int,
    phase_python: Path,
    work_root: Path,
) -> None:
    """Require a crashed launcher session and a fresh launcher recovery session."""
    gate_fd = _validated_gate_fd(gate_fd)
    project = work_root / "trial.gwxproj"
    phases = (
        (
            "producer",
            _phase_command(
                phase="producer",
                python=phase_python,
                gate_fd=gate_fd,
                checkout=checkout,
                work_root=work_root,
            ),
            -signal.SIGKILL,
        ),
        (
            "consumer",
            _phase_command(
                phase="consumer",
                python=phase_python,
                gate_fd=gate_fd,
                checkout=checkout,
                work_root=work_root,
                project=project,
            ),
            0,
        ),
    )
    for phase, command, expected_returncode in phases:
        try:
            completed = subprocess.run(
                command,
                cwd=work_root,
                capture_output=True,
                check=False,
                env=os.environ.copy(),
                pass_fds=(gate_fd,),
                text=False,
                timeout=120,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise GateError(f"technical-gate {phase} phase could not run") from exc
        if completed.returncode != expected_returncode:
            raise GateError(f"technical-gate {phase} phase failed")
        if phase == "producer":
            if project.is_symlink() or not project.is_file():
                raise GateError("technical-gate producer did not save a project")
            exported = work_root / "python-export.py"
            if exported.is_symlink() or not exported.is_file():
                raise GateError("technical-gate producer did not export Python")
            try:
                replay = subprocess.run(
                    (str(phase_python), str(exported)),
                    cwd=work_root,
                    capture_output=True,
                    check=False,
                    env=os.environ.copy(),
                    text=False,
                    timeout=120,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise GateError("technical-gate Python export could not run") from exc
            if replay.returncode != 0:
                raise GateError("technical-gate Python export failed")
            for name in (
                "launcher_import",
                "welcome",
                "try_sample",
                "crop",
                "asd",
                "save_project",
            ):
                checks[name] = True
        else:
            for name in ("project_reopen", "recovery", "worker_exit"):
                checks[name] = True


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _validated_gate_fd(value: object) -> int:
    """Reject descriptors that could name standard input/output/error streams."""
    if type(value) is not int or value < 3:
        raise GateError("technical-gate descriptor is invalid")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkout", type=Path, required=True)
    parser.add_argument("--work-root", type=Path, required=True)
    parser.add_argument("--result", type=Path)
    parser.add_argument("--phase", choices=("producer", "consumer"))
    parser.add_argument("--project", type=Path)
    parser.add_argument("--gate-fd", type=int)
    return parser


def _run_phase(arguments: argparse.Namespace) -> int:
    """Run one child-only launcher phase without writing the final gate record."""
    try:
        checkout = arguments.checkout.resolve(strict=True)
        work_root = arguments.work_root.resolve(strict=True)
        if not checkout.is_dir() or not work_root.is_dir():
            raise GateError("technical-gate phase paths are invalid")
        if arguments.phase == "producer":
            if arguments.project is not None:
                raise GateError("technical-gate producer cannot accept a project")
            _run_producer_launcher(checkout=checkout, work_root=work_root)
        elif arguments.phase == "consumer":
            if arguments.project is None:
                raise GateError("technical-gate consumer requires a project")
            _run_consumer_launcher(
                checkout=checkout,
                project=arguments.project.resolve(strict=True),
                work_root=work_root,
            )
        else:  # pragma: no cover - argparse constrains this internal value.
            raise GateError("technical-gate phase is invalid")
    except (OSError, GateError):
        return 1
    return 0


def _wait(app: object, predicate: object, label: str, timeout_s: float = 30.0) -> None:
    """Process Qt events until one externally observable gate condition holds."""
    from PySide6.QtCore import QEventLoop, QTimer

    if predicate():  # type: ignore[operator]
        return
    loop = QEventLoop()
    poll = QTimer()
    poll.setInterval(10)
    timed_out = [False]

    def check() -> None:
        if predicate():  # type: ignore[operator]
            loop.quit()

    def expire() -> None:
        timed_out[0] = True
        loop.quit()

    poll.timeout.connect(check)
    deadline = QTimer()
    deadline.setSingleShot(True)
    deadline.timeout.connect(expire)
    poll.start()
    deadline.start(int(timeout_s * 1000))
    loop.exec()
    poll.stop()
    deadline.stop()
    app.processEvents()  # type: ignore[attr-defined]
    if timed_out[0] or not predicate():  # type: ignore[operator]
        raise GateError(f"technical-gate timed out during {label}")


def _wait_for_sample_catalog(
    *, app: Any, window: Any, panel: Any, idle_state: object
) -> None:
    """Wait until the asynchronous Try Sample catalog enables inspection."""
    _wait(
        app,
        lambda: window.bridge.state is idle_state and panel.inspect_button.isEnabled(),
        "sample catalog",
    )


def _select_latest_timeseries_for_crop(
    *, app: Any, window: Any, idle_state: object
) -> str:
    """Select the latest TimeSeries through Sources before the recovery Crop."""
    object_id = next(
        (
            candidate.object_id
            for candidate in reversed(window.project.objects)
            if candidate.kind == "TimeSeries"
        ),
        None,
    )
    if not isinstance(object_id, str) or not object_id:
        raise GateError("technical-gate has no TimeSeries input for post-ASD Crop")
    item = window._find_object_item(window.source_tree, object_id)
    if item is None:
        raise GateError("technical-gate cannot select post-ASD Crop input")
    window.source_tree.setCurrentItem(item)
    _wait(
        app,
        lambda: (
            window.bridge.state is idle_state
            and window._current_object_id == object_id
            and window.crop_action.isEnabled()
        ),
        "post-ASD Crop input",
    )
    return object_id


def _click_dialog(
    dialog_type: type[Any],
    fill: Callable[[Any], None],
    action: Any,
    *,
    timeout_s: float = 15.0,
) -> None:
    """Accept one bounded modal operation dialog through its public controls."""
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication, QDialogButtonBox

    app = QApplication.instance()
    if not isinstance(app, QApplication):
        raise GateError("technical-gate operation dialog has no application")
    poll_timer = QTimer()
    poll_timer.setInterval(10)
    deadline_timer = QTimer()
    deadline_timer.setSingleShot(True)
    handled = [False]
    failure: list[BaseException] = []

    def current_dialog() -> Any | None:
        active = app.activeModalWidget()
        if isinstance(active, dialog_type):
            return active
        candidates = [
            widget
            for widget in app.topLevelWidgets()
            if isinstance(widget, dialog_type) and widget.isVisible()
        ]
        return candidates[0] if len(candidates) == 1 else None

    def stop() -> None:
        poll_timer.stop()
        deadline_timer.stop()

    def fail(error: BaseException) -> None:
        if not failure:
            failure.append(error)
        stop()
        dialog = current_dialog()
        if dialog is not None:
            dialog.reject()

    def accept() -> None:
        if handled[0] or failure:
            return
        dialog = current_dialog()
        if dialog is None:
            return
        try:
            fill(dialog)
            buttons = dialog.findChild(QDialogButtonBox)
            if buttons is None:
                raise GateError("technical-gate operation dialog has no buttons")
            button = buttons.button(QDialogButtonBox.StandardButton.Ok)
            if button is None:
                raise GateError("technical-gate operation dialog cannot be accepted")
            handled[0] = True
            stop()
            # A synthetic mouse event may be ignored by Cocoa while a nested
            # native modal loop is active.  The public button signal is the
            # deterministic cross-platform acceptance boundary.
            button.click()
        except BaseException as exc:
            fail(exc)

    def expire() -> None:
        fail(GateError("technical-gate operation dialog timed out"))

    poll_timer.timeout.connect(accept)
    deadline_timer.timeout.connect(expire)
    poll_timer.start()
    deadline_timer.start(max(1, int(timeout_s * 1000)))
    try:
        action.trigger()
    finally:
        stop()
    if failure:
        raise GateError("technical-gate operation dialog failed") from failure[0]
    if not handled[0]:
        raise GateError("technical-gate operation dialog did not appear")


def _remove_xdg_roots(work_root: Path) -> None:
    """Remove only the private XDG roots created by this gate process."""
    for name in ("cache", "config", "data", "state", "home", "mpl"):
        candidate = work_root / name
        if candidate.is_symlink():
            raise GateError("technical-gate XDG root is unsafe")
        shutil.rmtree(candidate, ignore_errors=False) if candidate.exists() else None


def _run_producer_launcher(*, checkout: Path, work_root: Path) -> None:
    """Create a newer recovery checkpoint, then intentionally kill its launcher."""
    _record_gate_stage(work_root, "producer", "bootstrap")
    import site

    from PySide6.QtCore import Qt, QTimer
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication

    import gwexpy_studio
    from gwexpy_studio.ui import app as launcher
    from gwexpy_studio.ui.bridge import BridgeState
    from gwexpy_studio.ui.dialogs import AsdDialog, CropDialog
    from gwexpy_studio.ui.io_panel import DataIOPanel
    from gwexpy_studio.ui.window import MainWindow

    verify_installed_module_path(
        module_path=Path(gwexpy_studio.__file__ or ""),
        site_roots=tuple(Path(path) for path in site.getsitepackages()),
        checkout_root=checkout,
    )
    _installed_identity()

    failure: list[BaseException] = []

    def drive() -> None:
        try:
            _record_gate_stage(work_root, "producer", "launcher")
            app = QApplication.instance()
            if not isinstance(app, QApplication):
                raise GateError("normal launcher did not create QApplication")
            windows = [
                item for item in app.topLevelWidgets() if isinstance(item, MainWindow)
            ]
            if len(windows) != 1:
                raise GateError("normal launcher did not create one main window")
            window = windows[0]
            _record_gate_stage(work_root, "producer", "welcome")
            _wait(
                app,
                lambda: window.isVisible() and window.welcome_panel.isVisible(),
                "welcome",
            )
            _record_gate_stage(work_root, "producer", "sample-availability")
            _wait(
                app,
                lambda: window.welcome_panel.try_sample_button.isEnabled(),
                "sample availability",
            )
            _record_gate_stage(work_root, "producer", "try-sample")
            QTest.mouseClick(
                window.welcome_panel.try_sample_button, Qt.MouseButton.LeftButton
            )
            _wait(app, lambda: window.open_data_panel is not None, "Try Sample")
            panel = window.open_data_panel
            if not isinstance(panel, DataIOPanel):
                raise GateError("Try Sample did not open data panel")
            _record_gate_stage(work_root, "producer", "sample-catalog")
            _wait_for_sample_catalog(
                app=app,
                window=window,
                panel=panel,
                idle_state=BridgeState.IDLE,
            )
            _record_gate_stage(work_root, "producer", "sample-inspection")
            QTest.mouseClick(panel.inspect_button, Qt.MouseButton.LeftButton)
            _wait(app, lambda: panel.confirm_button.isEnabled(), "sample inspection")
            QTest.mouseClick(panel.confirm_button, Qt.MouseButton.LeftButton)
            _record_gate_stage(work_root, "producer", "sample-read")
            _wait(
                app,
                lambda: (
                    window.bridge.state is BridgeState.IDLE
                    and len(window.project.objects) == 1
                ),
                "sample read",
            )

            def crop(dialog: CropDialog) -> None:
                dialog.start_edit.setText("0.125")
                dialog.end_edit.setText("0.875")

            _record_gate_stage(work_root, "producer", "crop-dialog")
            _click_dialog(CropDialog, crop, window.crop_action)
            _record_gate_stage(work_root, "producer", "crop")
            _wait(
                app,
                lambda: (
                    window.bridge.state is BridgeState.IDLE
                    and len(window.project.objects) == 2
                ),
                "crop",
            )

            def asd(dialog: AsdDialog) -> None:
                dialog.fftlength_edit.setText("0.125")
                dialog.overlap_edit.setText("0.0625")

            _record_gate_stage(work_root, "producer", "asd-dialog")
            _click_dialog(AsdDialog, asd, window.asd_action)
            _record_gate_stage(work_root, "producer", "asd")
            _wait(
                app,
                lambda: (
                    window.bridge.state is BridgeState.IDLE
                    and len(window.project.objects) == 3
                ),
                "ASD",
            )
            project = work_root / "trial.gwxproj"
            _record_gate_stage(work_root, "producer", "save-project")
            if project.exists() or not window.save_project_to(str(project)):
                raise GateError("Save Project could not start")
            _wait(
                app,
                lambda: window.bridge.state is BridgeState.IDLE and project.is_file(),
                "Save Project",
            )
            checkpoint_before = window._workspace_status.get("last_checkpoint_at")
            revision_before = window._workspace_status.get("revision")

            def post_save_crop(dialog: CropDialog) -> None:
                dialog.start_edit.setText("0.25")
                dialog.end_edit.setText("0.75")

            _record_gate_stage(work_root, "producer", "post-save-input")
            _select_latest_timeseries_for_crop(
                app=app,
                window=window,
                idle_state=BridgeState.IDLE,
            )
            _record_gate_stage(work_root, "producer", "post-save-crop-dialog")
            _click_dialog(CropDialog, post_save_crop, window.crop_action)
            _record_gate_stage(work_root, "producer", "recovery-checkpoint")
            _wait(
                app,
                lambda: (
                    window.bridge.state is BridgeState.IDLE
                    and len(window.project.objects) == 4
                    and window._workspace_status.get("revision") != revision_before
                    and window._workspace_status.get("last_checkpoint_at")
                    not in {None, checkpoint_before}
                    and window._workspace_status.get("recovery_error") is None
                ),
                "post-save recovery checkpoint",
            )
            if not project.is_file():
                raise GateError("technical-gate producer did not save a project")
            exported = work_root / "python-export.py"
            if exported.exists():
                raise GateError("Python export path is not fresh")
            _record_gate_stage(work_root, "producer", "python-export")
            window.export_to_file(str(exported))
            _wait(
                app,
                lambda: window.bridge.state is BridgeState.IDLE and exported.is_file(),
                "Python export",
            )
            _record_gate_stage(work_root, "producer", "intentional-crash")
            os.kill(os.getpid(), signal.SIGKILL)
        except BaseException as exc:
            failure.append(exc)
            QApplication.quit()

    app = QApplication.instance()
    if app is None:
        QApplication(_qapplication_arguments())
    elif not isinstance(app, QApplication):
        raise GateError("normal launcher cannot use the existing Qt application")
    QTimer.singleShot(0, drive)
    if launcher.main(()) != 0:
        raise GateError("normal launcher did not exit cleanly")
    if failure:
        raise GateError("technical-gate UI workflow failed") from failure[0]
    raise GateError("technical-gate producer exited before its intentional crash")


def _schedule_message_box_button(
    *,
    app: Any,
    owner: Any,
    label: str,
    title: str,
    required: bool,
    timeout_s: float = 15.0,
    on_visible: Callable[[], None] | None = None,
    on_selected: Callable[[], None] | None = None,
) -> _ScheduledMessageClick:
    """Click a title-bound message button without racing another modal.

    ``QTest.mouseClick`` is appropriate for ordinary visible controls, but an
    offscreen nested ``QMessageBox.exec()`` requires the native button signal
    to close reliably.  The timers are owned by the main window and bounded so
    a missing or malformed dialog cannot poll forever.
    """
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QMessageBox

    poll_timer = QTimer(owner)
    poll_timer.setInterval(10)
    deadline_timer = QTimer(owner)
    deadline_timer.setSingleShot(True)
    state = _ScheduledMessageClick(
        poll_timer=poll_timer,
        deadline_timer=deadline_timer,
    )

    def current_message() -> Any | None:
        active = app.activeModalWidget()
        if isinstance(active, QMessageBox) and active.windowTitle() == title:
            return active
        if active is not None:
            return None
        candidates = [
            widget
            for widget in app.topLevelWidgets()
            if (
                isinstance(widget, QMessageBox)
                and widget.isVisible()
                and widget.windowTitle() == title
            )
        ]
        return candidates[0] if len(candidates) == 1 else None

    def fail(error: BaseException) -> None:
        if state.error is None:
            state.error = error
        state.stop()
        message = current_message()
        if message is not None:
            message.reject()

    def expire() -> None:
        if not state.handled and required:
            fail(GateError("technical-gate expected message did not appear"))
        else:
            state.stop()

    def poll() -> None:
        if state.handled or state.error is not None:
            return
        try:
            message = current_message()
            if message is None:
                return
            if on_visible is not None:
                on_visible()
            button = next(
                (
                    candidate
                    for candidate in message.buttons()
                    if candidate.text().replace("&", "") == label
                ),
                None,
            )
            if button is None:
                fail(GateError("technical-gate expected message button is missing"))
                return
            state.handled = True
            state.stop()
            button.click()
            if on_selected is not None:
                on_selected()
        except BaseException as exc:
            fail(exc)

    poll_timer.timeout.connect(poll)
    deadline_timer.timeout.connect(expire)
    poll_timer.start()
    deadline_timer.start(max(1, int(timeout_s * 1000)))
    return state


def _run_consumer_launcher(*, checkout: Path, project: Path, work_root: Path) -> None:
    """Open a saved project in a fresh launcher and explicitly restore recovery."""
    _record_gate_stage(work_root, "consumer", "bootstrap")
    import site

    from PySide6.QtCore import Qt, QTimer
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication

    import gwexpy_studio
    from gwexpy_studio.ui import app as launcher
    from gwexpy_studio.ui.bridge import BridgeState
    from gwexpy_studio.ui.window import MainWindow

    verify_installed_module_path(
        module_path=Path(gwexpy_studio.__file__ or ""),
        site_roots=tuple(Path(path) for path in site.getsitepackages()),
        checkout_root=checkout,
    )
    _installed_identity()

    failure: list[BaseException] = []

    def drive() -> None:
        try:
            _record_gate_stage(work_root, "consumer", "launcher")
            app = QApplication.instance()
            if not isinstance(app, QApplication):
                raise GateError("normal launcher did not create QApplication")
            windows = [
                item for item in app.topLevelWidgets() if isinstance(item, MainWindow)
            ]
            if len(windows) != 1:
                raise GateError("normal launcher did not create one main window")
            window = windows[0]
            _record_gate_stage(work_root, "consumer", "project-reopen")
            _wait(app, lambda: window.isVisible(), "consumer welcome")
            _wait(
                app,
                lambda: (
                    window.bridge.state is BridgeState.IDLE
                    and len(window.project.objects) == 3
                    and bool(window._workspace_status.get("needs_restore"))
                ),
                "project reopen",
            )
            _wait(
                app,
                lambda: window.recovery_notice.isVisible(),
                "recovery notice",
            )
            _record_gate_stage(work_root, "consumer", "recovery-review")
            restore_message = _schedule_message_box_button(
                app=app,
                owner=window,
                label="Restore",
                title="Recover unfinished work",
                required=True,
                on_visible=lambda: _record_gate_stage(
                    work_root, "consumer", "recovery-candidate-visible"
                ),
                on_selected=lambda: _record_gate_stage(
                    work_root, "consumer", "recovery-candidate-selected"
                ),
            )
            discard_unsaved_message = _schedule_message_box_button(
                app=app,
                owner=window,
                label="Discard",
                title="Unsaved project",
                required=False,
                on_visible=lambda: _record_gate_stage(
                    work_root, "consumer", "unsaved-project-visible"
                ),
                on_selected=lambda: _record_gate_stage(
                    work_root, "consumer", "unsaved-project-discarded"
                ),
            )
            _record_gate_stage(work_root, "consumer", "recovery-review-trigger")
            QTest.mouseClick(
                window.review_recovery_notice_button, Qt.MouseButton.LeftButton
            )
            _record_gate_stage(work_root, "consumer", "recovery-review-requested")
            _wait(
                app,
                lambda: (
                    window.bridge.state is BridgeState.IDLE
                    and len(window.project.objects) == 4
                    and bool(window._workspace_status.get("needs_restore"))
                    and not window._workspace_status.get("project_path")
                ),
                "recovery restore",
            )
            restore_message.raise_if_failed()
            discard_unsaved_message.raise_if_failed()
            _record_gate_stage(work_root, "consumer", "recovery-restored")
            _record_gate_stage(work_root, "consumer", "data-restore")
            review_message = _schedule_message_box_button(
                app=app,
                owner=window,
                label="OK",
                title="Review restoration",
                required=True,
            )
            window.restore_project_action.trigger()
            _wait(
                app,
                lambda: (
                    window.bridge.state is BridgeState.IDLE
                    and len(window.project.objects) == 4
                    and not window._workspace_status.get("needs_restore")
                    and window._restore_progress is None
                ),
                "reviewed data restore",
            )
            review_message.raise_if_failed()
            _record_gate_stage(work_root, "consumer", "recovery-consumption")
            window.recoveries_action.trigger()
            _wait(
                app,
                lambda: (
                    window.bridge.state is BridgeState.IDLE
                    and not window._command_reserved
                ),
                "recovery consumption",
            )
            if app.activeModalWidget() is not None:
                raise GateError("technical-gate recovery candidate was not consumed")
            restored = work_root / "restored.gwxproj"
            _record_gate_stage(work_root, "consumer", "restored-project-save")
            if restored.exists() or not window.save_project_to(str(restored)):
                raise GateError("restored project could not be saved for clean exit")
            _wait(
                app,
                lambda: (
                    window.bridge.state is BridgeState.IDLE
                    and restored.is_file()
                    and window._workspace_status.get("project_path") == str(restored)
                    and not window._workspace_status.get("dirty")
                ),
                "restored project save",
            )
            _record_gate_stage(work_root, "consumer", "worker-exit")
            window.close()
            _wait(
                app,
                lambda: (
                    not window.isVisible()
                    and not window.bridge.worker_thread.isRunning()
                ),
                "worker exit",
            )
            _record_gate_stage(work_root, "consumer", "complete")
        except BaseException as exc:
            failure.append(exc)
            QApplication.quit()

    app = QApplication.instance()
    if app is None:
        QApplication(_qapplication_arguments())
    elif not isinstance(app, QApplication):
        raise GateError("normal launcher cannot use the existing Qt application")
    QTimer.singleShot(0, drive)
    if launcher.main((str(project),)) != 0:
        raise GateError("normal project launcher did not exit cleanly")
    if failure:
        raise GateError("technical-gate recovery workflow failed") from failure[0]


def main(argv: Sequence[str] | None = None) -> int:
    """Run the isolated installed-wheel gate and persist a bounded outcome."""
    parser = _parser()
    arguments = parser.parse_args(argv)
    if arguments.phase is not None:
        return _run_phase(arguments)
    if arguments.project is not None:
        parser.error("--project requires --phase consumer")
    if arguments.result is None:
        parser.error("--result is required without --phase")
    if arguments.gate_fd is None:
        parser.error("--gate-fd is required without --phase")
    checks = {name: False for name in _CHECK_NAMES}
    installed: dict[str, str] | None = None
    try:
        work_root = arguments.work_root.resolve(strict=True)
        result = arguments.result.resolve(strict=False)
        if result.parent != work_root or result.name != "technical-gate.json":
            raise GateError("technical-gate result path is outside its workspace")
        if result.exists() or result.is_symlink():
            raise GateError("technical-gate result path is not fresh")
        if sys.platform not in {"linux", "darwin"}:
            raise GateError("technical-gate recovery qualification requires POSIX")
        run_external_recovery_gate(
            checks=checks,
            checkout=arguments.checkout.resolve(strict=True),
            gate_fd=_validated_gate_fd(arguments.gate_fd),
            phase_python=Path(sys.executable),
            work_root=work_root,
        )
        installed = _installed_identity()
        prefix = os.environ.get("GWEXPY_STUDIO_SHM_PREFIX")
        if not prefix:
            raise GateError("technical-gate shared-memory prefix is unavailable")
        portable_cleanup = shared_memory_cleanup_probe(prefix)
        linux_namespace_clean = sys.platform != "linux" or not any(
            Path("/dev/shm").glob(f"{prefix}*")
        )
        checks["shared_memory_cleanup"] = portable_cleanup and linux_namespace_clean
        if not checks["shared_memory_cleanup"]:
            raise GateError("technical-gate shared memory was not cleaned up")
        _remove_xdg_roots(work_root)
        result.write_bytes(gate_result_json(checks, installed=installed))
        return 0
    except BaseException:
        try:
            _remove_xdg_roots(arguments.work_root)
        except (OSError, GateError):
            pass
        try:
            if installed is not None:
                arguments.result.write_bytes(
                    gate_result_json(checks, installed=installed)
                )
        except (OSError, GateError):
            pass
        return 1


if __name__ == "__main__":  # pragma: no cover - direct CLI invocation.
    raise SystemExit(main())
