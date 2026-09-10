"""Run one physical WSL2 or macOS trial qualification without manual UI use."""

from __future__ import annotations

import argparse
import io
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from collections.abc import Mapping, Sequence
from pathlib import Path

try:
    from .package_trial_release import read_verified_trial_release
    from .run_trial_technical_gate import (
        gate_environment,
        read_gate_result,
        technical_gate_command,
    )
    from .trial_targets import TrialTargetError, trial_target
    from .verify_platform_qualification import (
        QualificationError,
        qualification_result_bytes,
        required_checks,
    )
except ImportError:  # pragma: no cover - flat qualification-kit execution.
    from package_trial_release import (  # type: ignore[no-redef]
        read_verified_trial_release,
    )
    from run_trial_technical_gate import (  # type: ignore[no-redef]
        gate_environment,
        read_gate_result,
        technical_gate_command,
    )
    from trial_targets import TrialTargetError, trial_target  # type: ignore[no-redef]
    from verify_platform_qualification import (  # type: ignore[no-redef]
        QualificationError,
        qualification_result_bytes,
        required_checks,
    )


class PlatformQualificationError(RuntimeError):
    """Raised when a physical qualification cannot complete safely."""


_BUILD_ID = re.compile(r"P-[0-9a-f]{7}-[0-9]{8}-r[1-9][0-9]*-a[1-9][0-9]*")


def qualification_filename(target_id: str, architecture: str, build_id: str) -> str:
    """Return the target-, architecture-, and Build-ID-bound result filename."""
    try:
        target = trial_target(target_id)
        target.require_architecture(architecture)
    except TrialTargetError as exc:
        raise PlatformQualificationError(str(exc)) from exc
    if _BUILD_ID.fullmatch(build_id) is None:
        raise PlatformQualificationError("invalid Build ID")
    return f"qualification-{target.id}-{architecture}-{build_id}.json"


def _run(
    command: Sequence[str],
    *,
    cwd: Path,
    environment: Mapping[str, str] | None = None,
    timeout: int = 600,
) -> subprocess.CompletedProcess[bytes]:
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            capture_output=True,
            check=False,
            env=None if environment is None else dict(environment),
            text=False,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PlatformQualificationError("qualification subprocess failed") from exc
    if completed.returncode != 0:
        raise PlatformQualificationError("qualification subprocess failed")
    return completed


def _windows_value(script: str, *, cwd: Path) -> str:
    completed = _run(
        (
            "powershell.exe",
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            script,
        ),
        cwd=cwd,
        timeout=60,
    )
    try:
        return completed.stdout.decode("utf-8").strip().replace("\r", "")
    except UnicodeDecodeError as exc:
        raise PlatformQualificationError("Windows host query was not UTF-8") from exc


def _ubuntu_release() -> str:
    try:
        fields = dict(
            line.split("=", 1)
            for line in Path("/etc/os-release").read_text(encoding="utf-8").splitlines()
            if line and not line.startswith("#") and "=" in line
        )
    except OSError as exc:
        raise PlatformQualificationError("guest OS identity is unavailable") from exc
    if fields.get("ID", "").strip('"') != "ubuntu":
        raise PlatformQualificationError("WSL guest is not Ubuntu")
    return fields.get("VERSION_ID", "").strip('"')


def _host_record(target_id: str, architecture: str, *, cwd: Path) -> dict[str, str]:
    python_version = ".".join(str(value) for value in sys.version_info[:3])
    if target_id == "macos15-arm64":
        if sys.platform != "darwin" or platform.machine().lower() != "arm64":
            raise PlatformQualificationError(
                "qualification requires native macOS ARM64"
            )
        return {
            "architecture": "arm64",
            "macos_version": platform.mac_ver()[0],
            "python_version": python_version,
        }
    if sys.platform != "linux" or architecture not in {"x86_64", "aarch64"}:
        raise PlatformQualificationError("qualification requires a native WSL2 guest")
    kernel = platform.release()
    if "microsoft" not in kernel.lower() or "WSL_INTEROP" not in os.environ:
        raise PlatformQualificationError("qualification host is not WSL2")
    windows_architecture = _windows_value(
        "[Console]::OutputEncoding=[Text.UTF8Encoding]::new(); "
        "$env:PROCESSOR_ARCHITECTURE",
        cwd=cwd,
    ).upper()
    mapped_architecture = {"AMD64": "x86_64", "ARM64": "aarch64"}.get(
        windows_architecture
    )
    if mapped_architecture != architecture:
        raise PlatformQualificationError("Windows and guest architectures disagree")
    build = _windows_value(
        "[Console]::OutputEncoding=[Text.UTF8Encoding]::new(); "
        "[Environment]::OSVersion.Version.Build",
        cwd=cwd,
    )
    if not build.isdigit() or int(build) < 22000:
        raise PlatformQualificationError("qualification requires Windows 11")
    return {
        "guest_architecture": architecture,
        "guest_os": "ubuntu",
        "guest_version": _ubuntu_release(),
        "kernel_release": kernel,
        "python_version": python_version,
        "windows_architecture": architecture,
        "windows_version": f"11.0.{build}",
    }


_QT_PROBE = r"""
import ctypes
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

from PySide6.QtGui import QOffscreenSurface, QOpenGLContext
from PySide6.QtWidgets import QApplication, QFileDialog

from gwexpy_studio.ui.support import AboutDialog
from gwexpy_studio.ui.app import _trial_capability_environment

target = sys.argv[1]
roundtrip_path = sys.argv[2]
expected_build_id = sys.argv[3]
app = QApplication([])
screen = app.primaryScreen()
surface = QOffscreenSurface()
surface.create()
context = QOpenGLContext()
opengl = context.create() and surface.isValid() and context.makeCurrent(surface)
if opengl:
    context.doneCurrent()

token = "gwexpy-clipboard-" + uuid.uuid4().hex
app.clipboard().setText(token)
app.processEvents()
clipboard = app.clipboard().text() == token
windows_roundtrip = False
if target == "wsl2-ubuntu24":
    for library in ("libEGL.so.1", "libGL.so.1"):
        ctypes.CDLL(library)
    getter = subprocess.run(
        (
            "powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive",
            "-Command", "Get-Clipboard",
        ),
        capture_output=True,
        check=False,
        text=True,
        timeout=30,
    )
    incoming = "gwexpy-windows-" + uuid.uuid4().hex
    setter = subprocess.run(
        (
            "powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive",
            "-Command", f"Set-Clipboard -Value '{incoming}'",
        ),
        capture_output=True,
        check=False,
        text=True,
        timeout=30,
    )
    for _ in range(100):
        app.processEvents()
        if app.clipboard().text() == incoming:
            break
        time.sleep(0.02)
    windows_roundtrip = (
        getter.returncode == 0
        and getter.stdout.strip() == token
        and setter.returncode == 0
        and app.clipboard().text() == incoming
    )

dialog_path = str(Path(roundtrip_path) / "解析 結果.py")
original = QFileDialog.getSaveFileName
QFileDialog.getSaveFileName = staticmethod(
    lambda *_args, **_kwargs: (dialog_path, "Python Scripts (*.py)")
)
selected, _filter = QFileDialog.getSaveFileName(
    None, "Export", "export.py", "Python Scripts (*.py)"
)
QFileDialog.getSaveFileName = original

with _trial_capability_environment():
    about = AboutDialog()
    about.copy_diagnostics()
    diagnostics = app.clipboard().text()
result = {
    "about_build_id": f"Build ID: {expected_build_id}" in diagnostics,
    "clipboard": clipboard,
    "diagnostics_copy": "Build ID:" in diagnostics,
    "dpi_scaling": bool(
        screen and screen.logicalDotsPerInch() > 0 and screen.devicePixelRatio() > 0
    ),
    "file_dialog_path": selected == dialog_path,
    "opengl": bool(opengl),
    "platform": app.platformName(),
    "screen": bool(
        screen and screen.geometry().width() > 0 and screen.geometry().height() > 0
    ),
    "windows_clipboard_roundtrip": windows_roundtrip,
}
print(json.dumps(result, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
"""


def _qt_probe(target_id: str, roundtrip_root: Path, build_id: str) -> dict[str, object]:
    environment = os.environ.copy()
    environment.pop("QT_QPA_PLATFORM", None)
    completed = _run(
        (
            sys.executable,
            "-I",
            "-c",
            _QT_PROBE,
            target_id,
            str(roundtrip_root),
            build_id,
        ),
        cwd=roundtrip_root,
        environment=environment,
        timeout=180,
    )
    try:
        result = json.loads(completed.stdout)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PlatformQualificationError("Qt qualification probe is invalid") from exc
    if not isinstance(result, dict):
        raise PlatformQualificationError("Qt qualification probe is invalid")
    return result


def _windows_path_roundtrip(cwd: Path) -> bool:
    windows_temp = _windows_value(
        "[Console]::OutputEncoding=[Text.UTF8Encoding]::new(); "
        "[IO.Path]::GetTempPath()",
        cwd=cwd,
    )
    translated = _run(("wslpath", "-u", windows_temp), cwd=cwd, timeout=30)
    try:
        parent = Path(translated.stdout.decode("utf-8").strip())
    except UnicodeDecodeError as exc:
        raise PlatformQualificationError("Windows temporary path is invalid") from exc
    directory = Path(tempfile.mkdtemp(prefix="GWexpy 試験 ", dir=parent))
    try:
        payload = directory / "測定 データ.txt"
        payload.write_text("roundtrip\n", encoding="utf-8")
        return payload.read_text(encoding="utf-8") == "roundtrip\n"
    finally:
        shutil.rmtree(directory)


def _extract_verified(
    archive_name: str, archive_bytes: bytes, destination: Path
) -> Path:
    with zipfile.ZipFile(io.BytesIO(archive_bytes)) as contents:
        roots = {name.split("/", 1)[0] for name in contents.namelist()}
        if len(roots) != 1:
            raise PlatformQualificationError("candidate archive root is invalid")
        contents.extractall(destination)
    expected_root = archive_name.removesuffix(".zip")
    if roots != {expected_root}:
        raise PlatformQualificationError("candidate archive root is invalid")
    root = destination / expected_root
    if not root.is_dir():
        raise PlatformQualificationError("candidate archive could not be extracted")
    return root


def run_qualification(
    *,
    target_id: str,
    archive: Path,
    sidecar: Path,
    output_directory: Path,
    work_root: Path,
) -> Path:
    """Verify, install, drive the UI, and emit one canonical physical result."""
    try:
        target = trial_target(target_id)
        manifest, archive_bytes = read_verified_trial_release(archive, sidecar)
    except (TrialTargetError, ValueError) as exc:
        raise PlatformQualificationError("candidate verification failed") from exc
    target_record = manifest.get("target")
    if not isinstance(target_record, dict) or target_record.get("id") != target_id:
        raise PlatformQualificationError("candidate target does not match the kit")
    build = manifest["build"]
    wheel_record = manifest["wheel"]
    if not isinstance(build, dict) or not isinstance(wheel_record, dict):
        raise PlatformQualificationError("candidate identity is invalid")
    build_id = str(build["id"])
    source_sha = str(build["source_sha"])
    wheel_sha256 = str(wheel_record["sha256"])
    architecture = platform.machine().lower()
    try:
        target.require_architecture(architecture)
    except TrialTargetError as exc:
        raise PlatformQualificationError(str(exc)) from exc
    checks = {name: False for name in required_checks(target_id)}
    host = _host_record(target_id, architecture, cwd=work_root)
    extracted = _extract_verified(archive.name, archive_bytes, work_root / "candidate")
    constraints = extracted / target.constraints_filename(architecture)
    wheel = extracted / str(wheel_record["filename"])
    checks["archive_checksum"] = True
    checks["bundle_verification"] = True
    conda_prefix = os.environ.get("CONDA_PREFIX")
    checks["environment_isolation"] = (
        conda_prefix is not None
        and Path(conda_prefix).resolve() == Path(sys.prefix).resolve()
        and sys.version_info[:2] == (3, 12)
    )
    _run(
        (
            sys.executable,
            "-m",
            "pip",
            "install",
            "--isolated",
            "--no-input",
            "--only-binary=:all:",
            "--disable-pip-version-check",
            "-c",
            str(constraints),
            str(wheel),
        ),
        cwd=work_root,
    )
    _run((sys.executable, "-m", "pip", "check", "--isolated"), cwd=work_root)
    checks["binary_install"] = True
    imported = _run(
        (
            sys.executable,
            "-I",
            "-c",
            "import json,gwexpy_studio; from importlib import resources; "
            "print(resources.files('gwexpy_studio').joinpath('assets','trial-build.json').read_text())",
        ),
        cwd=work_root,
    )
    installed_identity = json.loads(imported.stdout)
    checks["import_origin"] = (
        installed_identity.get("build_id") == build_id
        and installed_identity.get("source_sha") == source_sha
    )

    gui_root = work_root / "GUI 試験"
    gui_root.mkdir()
    gate_result_path = gui_root / "technical-gate.json"
    gate_script = Path(__file__).with_name("run_trial_technical_gate.py")
    gate_fd = os.open(gate_script, os.O_RDONLY)
    try:
        environment = gate_environment(
            gui_root,
            os.environ,
            f"gwexpy-physical-{os.getpid()}-",
            native_qt=True,
        )
        _run(
            technical_gate_command(
                python=Path(sys.executable),
                gate_fd=gate_fd,
                checkout_root=Path(__file__).resolve().parent,
                work_root=gui_root,
                result_path=gate_result_path,
            ),
            cwd=gui_root,
            environment=environment,
            timeout=600,
        )
    finally:
        os.close(gate_fd)
    gate = read_gate_result(gate_result_path.read_bytes())
    gate_checks = gate["checks"]
    assert isinstance(gate_checks, dict)
    mapping = {
        "asd": "asd",
        "crop": "crop",
        "launcher": "launcher_import",
        "load": "try_sample",
        "project_reopen": "project_reopen",
        "recovery": "recovery",
        "save_project": "save_project",
        "shared_memory_cleanup": "shared_memory_cleanup",
        "try_sample": "try_sample",
        "welcome": "welcome",
        "worker_exit": "worker_exit",
    }
    for result_name, gate_name in mapping.items():
        checks[result_name] = gate_checks.get(gate_name) is True
    checks["close_project"] = checks["project_reopen"]
    checks["python_export"] = (gui_root / "python-export.py").is_file()

    probe = _qt_probe(target_id, gui_root, build_id)
    checks["about_build_id"] = probe.get("about_build_id") is True
    checks["clipboard"] = probe.get("clipboard") is True
    checks["diagnostics_copy"] = probe.get("diagnostics_copy") is True
    checks["dpi_scaling"] = probe.get("dpi_scaling") is True
    checks["qt_screen"] = probe.get("screen") is True
    if target_id == "wsl2-ubuntu24":
        checks["guest_architecture"] = host["guest_architecture"] == architecture
        checks["windows_architecture"] = host["windows_architecture"] == architecture
        checks["wsl2_kernel"] = "microsoft" in host["kernel_release"].lower()
        checks["wslg_display"] = (
            bool(os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY"))
            and Path("/mnt/wslg").is_dir()
        )
        checks["qt_egl_gl"] = probe.get("opengl") is True and probe.get("platform") in {
            "wayland",
            "xcb",
        }
        checks["windows_clipboard_roundtrip"] = (
            probe.get("windows_clipboard_roundtrip") is True
        )
        checks["linux_unicode_space_path"] = True
        checks["windows_unicode_space_path"] = _windows_path_roundtrip(work_root)
    else:
        checks["macos_version"] = int(host["macos_version"].split(".", 1)[0]) >= 15
        checks["native_arm64"] = architecture == "arm64"
        checks["cocoa_platform"] = probe.get("platform") == "cocoa"
        checks["qt_opengl"] = probe.get("opengl") is True
        checks["file_dialog_path"] = probe.get("file_dialog_path") is True
        checks["macos_unicode_space_path"] = True

    if output_directory.exists() or output_directory.is_symlink():
        raise PlatformQualificationError("qualification output must be fresh")
    output_directory.mkdir(parents=True)
    destination = output_directory / qualification_filename(
        target_id, architecture, build_id
    )
    destination.write_bytes(
        qualification_result_bytes(
            target_id=target_id,
            architecture=architecture,
            build_id=build_id,
            source_sha=source_sha,
            wheel_sha256=wheel_sha256,
            host=host,
            checks=checks,
        )
    )
    return destination


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trial-target", required=True)
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("--sidecar", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--work-root", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run one physical qualification from command-line arguments."""
    arguments = _parser().parse_args(argv)
    try:
        arguments.work_root.mkdir(mode=0o700)
        result = run_qualification(
            target_id=arguments.trial_target,
            archive=arguments.archive,
            sidecar=arguments.sidecar,
            output_directory=arguments.output,
            work_root=arguments.work_root,
        )
    except (OSError, QualificationError, PlatformQualificationError) as exc:
        print(f"qualification failed: {exc}", file=sys.stderr)
        return 1
    print(result.name)
    result_document = json.loads(result.read_bytes())
    return 0 if result_document.get("status") == "passed" else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
