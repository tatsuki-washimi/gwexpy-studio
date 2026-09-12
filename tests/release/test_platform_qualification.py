"""Physical platform qualification evidence contracts."""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.unit

BUILD_ID = "P-abcdef0-20260910-r8-a1"
SOURCE_SHA = "abcdef0" + "1" * 33
WHEEL_SHA = "2" * 64


def _module():
    return importlib.import_module("scripts.verify_platform_qualification")


def _result(
    target_id: str,
    architecture: str,
    *,
    build_id: str = BUILD_ID,
    source_sha: str = SOURCE_SHA,
    wheel_sha: str = WHEEL_SHA,
) -> bytes:
    module = _module()
    checks = {name: True for name in module.required_checks(target_id)}
    if target_id == "wsl2-ubuntu24":
        host = {
            "guest_architecture": architecture,
            "guest_os": "ubuntu",
            "guest_version": "24.04",
            "kernel_release": "6.6.87.2-microsoft-standard-WSL2",
            "python_version": "3.12.12",
            "windows_architecture": architecture,
            "windows_version": "11.0.26100",
        }
    else:
        host = {
            "architecture": "arm64",
            "macos_version": "15.7.1",
            "python_version": "3.12.12",
        }
    return module.qualification_result_bytes(
        target_id=target_id,
        architecture=architecture,
        build_id=build_id,
        source_sha=source_sha,
        wheel_sha256=wheel_sha,
        host=host,
        checks=checks,
    )


def test_wsl2_summary_requires_both_architectures_and_binds_identity() -> None:
    module = _module()
    results = [_result("wsl2-ubuntu24", "x86_64"), _result("wsl2-ubuntu24", "aarch64")]

    summary = module.qualification_summary_bytes(results)
    document = json.loads(summary)

    assert document == {
        "build_id": BUILD_ID,
        "results": [
            {
                "architecture": "aarch64",
                "sha256": hashlib.sha256(results[1]).hexdigest(),
            },
            {
                "architecture": "x86_64",
                "sha256": hashlib.sha256(results[0]).hexdigest(),
            },
        ],
        "schema": 1,
        "source_sha": SOURCE_SHA,
        "status": "passed",
        "target_id": "wsl2-ubuntu24",
        "wheel_sha256": WHEEL_SHA,
    }
    assert summary.endswith(b"\n")


def test_macos_summary_accepts_one_arm64_result() -> None:
    module = _module()

    summary = json.loads(
        module.qualification_summary_bytes([_result("macos15-arm64", "arm64")])
    )

    assert summary["target_id"] == "macos15-arm64"
    assert summary["results"][0]["architecture"] == "arm64"
    assert summary["status"] == "passed"


def test_campaign_summary_requires_all_three_physical_configurations() -> None:
    module = _module()
    mac_build_id = "P-abcdef0-20260910-r9-a1"
    mac_wheel_sha = "3" * 64
    results = [
        _result("wsl2-ubuntu24", "x86_64"),
        _result("wsl2-ubuntu24", "aarch64"),
        _result(
            "macos15-arm64",
            "arm64",
            build_id=mac_build_id,
            wheel_sha=mac_wheel_sha,
        ),
    ]

    summary = module.qualification_campaign_summary_bytes(results)
    document = json.loads(summary)

    assert document["source_sha"] == SOURCE_SHA
    assert document["status"] == "passed"
    assert document["schema"] == 1
    assert [target["target_id"] for target in document["targets"]] == [
        "macos15-arm64",
        "wsl2-ubuntu24",
    ]
    assert document["targets"][0]["build_id"] == mac_build_id
    assert document["targets"][0]["wheel_sha256"] == mac_wheel_sha
    assert len(document["targets"][1]["results"]) == 2


@pytest.mark.parametrize("mutation", ["missing_target", "source_mismatch"])
def test_campaign_summary_rejects_incomplete_or_mixed_source_evidence(
    mutation: str,
) -> None:
    module = _module()
    results = [
        _result("wsl2-ubuntu24", "x86_64"),
        _result("wsl2-ubuntu24", "aarch64"),
        _result("macos15-arm64", "arm64", build_id="P-abcdef0-20260910-r9-a1"),
    ]
    if mutation == "missing_target":
        results.pop()
    else:
        results[-1] = _result(
            "macos15-arm64",
            "arm64",
            build_id="P-4444444-20260910-r9-a1",
            source_sha="4" * 40,
        )

    with pytest.raises(module.QualificationError):
        module.qualification_campaign_summary_bytes(results)


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "failed", "identity"])
def test_summary_rejects_incomplete_or_untrusted_results(mutation: str) -> None:
    module = _module()
    first = _result("wsl2-ubuntu24", "x86_64")
    second = _result("wsl2-ubuntu24", "aarch64")
    values = [first, second]
    if mutation == "missing":
        values = values[:1]
    elif mutation == "duplicate":
        values = [first, first]
    else:
        document = json.loads(second)
        if mutation == "failed":
            check = next(iter(document["checks"]))
            document["checks"][check] = False
            document["status"] = "failed"
        else:
            document["source_sha"] = "3" * 40
        second = (
            json.dumps(document, sort_keys=True, separators=(",", ":")).encode() + b"\n"
        )
        values = [first, second]

    with pytest.raises(module.QualificationError):
        module.qualification_summary_bytes(values)


def test_result_rejects_paths_and_unknown_check_names() -> None:
    module = _module()
    checks = {name: True for name in module.required_checks("macos15-arm64")}
    checks["unexpected"] = True

    with pytest.raises(module.QualificationError):
        module.qualification_result_bytes(
            target_id="macos15-arm64",
            architecture="arm64",
            build_id=BUILD_ID,
            source_sha=SOURCE_SHA,
            wheel_sha256=WHEEL_SHA,
            host={
                "architecture": "arm64",
                "macos_version": "/Users/alice/private",
                "python_version": "3.12.12",
            },
            checks=checks,
        )


def _schema2_host(target_id: str, architecture: str) -> dict[str, str]:
    if target_id == "ubuntu24-x86_64":
        return {
            "architecture": architecture,
            "host_os": "ubuntu",
            "os_version": "24.04",
            "python_version": "3.12.14",
            "qt_backend": "wayland",
        }
    if target_id == "debian13-x86_64":
        return {
            "architecture": architecture,
            "host_os": "debian",
            "os_version": "13",
            "python_version": "3.12.14",
            "qt_backend": "xcb",
        }
    if target_id == "wsl2-ubuntu24":
        return {
            "guest_architecture": architecture,
            "guest_os": "ubuntu",
            "guest_version": "24.04",
            "kernel_release": "6.6.87-microsoft-standard-WSL2",
            "python_version": "3.12.14",
            "qt_backend": "wayland",
            "windows_architecture": architecture,
            "windows_version": "11.0.26100",
        }
    return {
        "architecture": architecture,
        "macos_version": "15.6",
        "python_version": "3.12.14",
        "qt_backend": "cocoa",
    }


@pytest.mark.parametrize(
    ("target_id", "architecture"),
    [
        ("ubuntu24-x86_64", "x86_64"),
        ("debian13-x86_64", "x86_64"),
        ("wsl2-ubuntu24", "x86_64"),
        ("wsl2-ubuntu24", "aarch64"),
        ("macos15-arm64", "arm64"),
    ],
)
def test_schema2_binds_all_five_host_variants(
    target_id: str, architecture: str
) -> None:
    module = _module()
    automated = {
        name: True for name in module.required_schema2_automated_checks(target_id)
    }
    owners = {
        name: True for name in module.required_schema2_owner_confirmations(target_id)
    }
    raw = module.qualification2_result_bytes(
        target_id=target_id,
        architecture=architecture,
        build_id=BUILD_ID,
        source_sha=SOURCE_SHA,
        wheel_sha256=WHEEL_SHA,
        zip_sha256="4" * 64,
        kit_manifest_sha256="5" * 64,
        host=_schema2_host(target_id, architecture),
        automated_checks=automated,
        owner_confirmations=owners,
        cleanup=True,
        stage="complete",
    )
    document = module.read_qualification2_result(raw, require_passed=True)
    assert document["schema"] == 2
    assert document["status"] == "passed"
    assert "hostname" not in raw.decode()
    assert "username" not in raw.decode()


def test_schema2_owner_false_and_unanswered_never_pass() -> None:
    module = _module()
    automated = {
        name: True for name in module.required_schema2_automated_checks("macos15-arm64")
    }
    owners = {
        name: True
        for name in module.required_schema2_owner_confirmations("macos15-arm64")
    }
    owners["normal_scale_operation"] = False
    failed = module.qualification2_result_bytes(
        target_id="macos15-arm64",
        architecture="arm64",
        build_id=BUILD_ID,
        source_sha=SOURCE_SHA,
        wheel_sha256=WHEEL_SHA,
        zip_sha256="4" * 64,
        kit_manifest_sha256="5" * 64,
        host=_schema2_host("macos15-arm64", "arm64"),
        automated_checks=automated,
        owner_confirmations=owners,
        cleanup=True,
        stage="owner",
        error_code="owner-false",
    )
    assert json.loads(failed)["status"] == "failed"
    with pytest.raises(module.QualificationError):
        module.read_qualification2_result(failed, require_passed=True)


def test_schema2_rejects_non_312_python_on_macos() -> None:
    module = _module()
    automated = {
        name: True for name in module.required_schema2_automated_checks("macos15-arm64")
    }
    owners = {
        name: True
        for name in module.required_schema2_owner_confirmations("macos15-arm64")
    }
    host = _schema2_host("macos15-arm64", "arm64")
    host["python_version"] = "3.11.9"
    with pytest.raises(module.QualificationError):
        module.qualification2_result_bytes(
            target_id="macos15-arm64",
            architecture="arm64",
            build_id=BUILD_ID,
            source_sha=SOURCE_SHA,
            wheel_sha256=WHEEL_SHA,
            zip_sha256="4" * 64,
            kit_manifest_sha256="5" * 64,
            host=host,
            automated_checks=automated,
            owner_confirmations=owners,
            cleanup=True,
            stage="complete",
        )


def test_owner_prompt_keeps_completed_answers_on_eof() -> None:
    import scripts.run_platform_qualification as runner

    answers: dict[str, bool | None] = {}
    values = iter(["y", "n"])

    def reader(_prompt: str) -> str:
        try:
            return next(values)
        except StopIteration:
            raise EOFError

    with pytest.raises(runner.PlatformQualificationError, match="owner-unanswered"):
        runner._owner_prompt("ubuntu24-x86_64", reader=reader, answers=answers)
    assert answers["native_file_dialog"] is True
    assert answers["normal_scale_display"] is False
    assert "normal_scale_operation" not in answers


def test_sealed_gate_descriptor_is_read_only_and_unlinked(tmp_path: Path) -> None:
    import scripts.run_platform_qualification as runner

    with runner._sealed_gate_script(b"gate", tmp_path) as descriptor:
        assert not Path(f"/proc/self/fd/{descriptor}").resolve().exists()
        with pytest.raises(OSError):
            os.write(descriptor, b"tamper")
        assert os.read(descriptor, 4) == b"gate"


def test_bootstrap_read_rejects_a_byte_race(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import scripts.run_platform_qualification as runner

    path = tmp_path / "stable.bin"
    path.write_bytes(b"stable")
    original_read = runner.os.read
    changed = False

    def read_and_replace(descriptor: int, size: int) -> bytes:
        nonlocal changed
        value = original_read(descriptor, size)
        if not changed:
            path.write_bytes(b"raced!")
            changed = True
        return value

    monkeypatch.setattr(runner.os, "read", read_and_replace)
    with pytest.raises(RuntimeError, match="changed"):
        runner._bootstrap_read(path)


def test_missing_kit_writes_bounded_untrusted_result(tmp_path: Path) -> None:
    import scripts.run_platform_qualification as runner

    output = tmp_path / "results"
    with pytest.raises(runner.PlatformQualificationError):
        runner.run_qualification(
            target_id="ubuntu24-x86_64",
            kit=tmp_path / "missing-kit",
            output_directory=output,
            work_root=tmp_path / "work",
        )
    document = _module().read_qualification2_result(
        (output / "qualification-untrusted.json").read_bytes()
    )
    assert document["status"] == "failed"
    assert document["target_id"] is None


def test_result_write_never_overwrites_existing_destination(tmp_path: Path) -> None:
    import scripts.run_platform_qualification as runner

    destination = tmp_path / "result.json"
    destination.write_bytes(b"original")
    with pytest.raises(runner.PlatformQualificationError, match="result-save-failed"):
        runner._write_result_once(destination, b"replacement")
    assert destination.read_bytes() == b"original"


def test_run_executes_a_real_bounded_child(tmp_path: Path) -> None:
    import scripts.run_platform_qualification as runner

    completed = runner._run(
        (runner.sys.executable, "-I", "-c", "print('qualification-child')"),
        cwd=tmp_path,
        timeout=10,
    )
    assert completed.stdout.strip() == b"qualification-child"


@pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX-only")
def test_terminate_owned_process_cleans_descendant_after_leader_exit() -> None:
    import scripts.run_platform_qualification as runner

    child_code = (
        "import subprocess,sys,time; "
        "subprocess.Popen([sys.executable, '-I', '-c', "
        "'import time; time.sleep(60)']); "
        "time.sleep(60)"
    )
    process = subprocess.Popen(
        [runner.sys.executable, "-I", "-c", child_code],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        # The first child has already created the grandchild by the time the
        # short delay elapses.  Killing only the leader must leave that
        # grandchild behind, which is the lifecycle bug this regression covers.
        time.sleep(0.2)
        process.terminate()
        process.wait(timeout=5)
        runner._terminate_owned_process(process)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                os.killpg(process.pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            pytest.fail("owned process group survived leader exit")
    finally:
        runner._terminate_owned_process(process)


def test_owner_child_handshake_script_compiles_and_uses_native_project_actions() -> (
    None
):
    import scripts.run_platform_qualification as runner

    compile(runner._OWNER_CHILD, "<owner-child>", "exec")
    assert "open_project_action.trigger()" in runner._OWNER_CHILD
    assert "worker_thread" in runner._OWNER_CHILD


def test_launcher_preflight_rejects_shadow_module_before_helper_imports(
    tmp_path: Path,
) -> None:
    import scripts.run_platform_qualification as runner

    Path("/tmp/qualification-shadow-executed").unlink(missing_ok=True)
    kit = tmp_path / "kit"
    kit.mkdir()
    controller_bytes = Path(runner.__file__).read_bytes()
    source = (
        json.dumps(
            {
                "entries": [
                    {
                        "path": "scripts/run_platform_qualification.py",
                        "sha256": hashlib.sha256(controller_bytes).hexdigest(),
                    }
                ]
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        + b"\n"
    )
    archive_name = "candidate.zip"
    sidecar_name = "candidate.zip.sha256"
    runner_bytes = runner._bootstrap_runner_bytes(
        "ubuntu24-x86_64", archive_name, sidecar_name
    )
    manifest = {
        "archive": {
            "filename": archive_name,
            "sha256": hashlib.sha256(b"archive").hexdigest(),
        },
        "build_id": BUILD_ID,
        "legacy_fixture": False,
        "runner": {
            "filename": "run-qualification.sh",
            "sha256": hashlib.sha256(runner_bytes).hexdigest(),
        },
        "schema": 2,
        "scripts": [
            {
                "filename": "run_platform_qualification.py",
                "sha256": hashlib.sha256(controller_bytes).hexdigest(),
                "source_path": "scripts/run_platform_qualification.py",
            }
        ],
        "sidecar": {
            "filename": sidecar_name,
            "sha256": hashlib.sha256(b"sidecar").hexdigest(),
        },
        "source_manifest_sha256": hashlib.sha256(source).hexdigest(),
        "source_sha": SOURCE_SHA,
        "target_id": "ubuntu24-x86_64",
        "wheel": {"filename": "trial.whl", "sha256": "1" * 64},
    }
    (kit / "QUALIFICATION-KIT.json").write_bytes(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    )
    (kit / "SOURCE-MANIFEST.json").write_bytes(source)
    (kit / archive_name).write_bytes(b"archive")
    (kit / sidecar_name).write_bytes(b"sidecar")
    (kit / "run_platform_qualification.py").write_bytes(controller_bytes)
    (kit / "run-qualification.sh").write_bytes(runner_bytes)
    (kit / "json.py").write_text(
        "Path('/tmp/qualification-shadow-executed').write_text('bad')\n",
        encoding="utf-8",
    )
    launcher = kit / "run-qualification.sh"
    launcher.chmod(0o755)
    fake_conda = tmp_path / "conda"
    fake_conda.write_text('#!/bin/sh\nshift 4\nexec "$@"\n', encoding="ascii")
    fake_conda.chmod(0o755)
    completed = subprocess.run(
        [str(launcher)],
        cwd=tmp_path,
        env={
            **{
                key: value
                for key, value in os.environ.items()
                if key not in {"PYTHONPATH", "PYTHONNOUSERSITE"}
            },
            "CONDA_EXE": str(fake_conda),
        },
        capture_output=True,
        check=False,
    )
    assert completed.returncode != 0
    assert b"ModuleNotFoundError" not in completed.stderr
    result = kit / "results" / "qualification-untrusted.json"
    assert result.is_file(), completed.stderr.decode()
    document = _module().read_qualification2_result(result.read_bytes())
    assert document["status"] == "failed"
    assert document["target_id"] is None
    assert not Path("/tmp/qualification-shadow-executed").exists()


def test_schema2_untrusted_diagnostic_does_not_assert_identity() -> None:
    module = _module()
    raw = module.qualification2_untrusted_failure_bytes(
        stage="preflight", error_code="checksum-mismatch"
    )
    document = module.read_qualification2_result(raw)
    assert document["source_sha"] is None
    assert document["target_id"] is None
    with pytest.raises(module.QualificationError):
        module.read_qualification2_result(raw, require_passed=True)


def test_schema2_untrusted_environment_failure_with_cleanup_round_trips() -> None:
    module = _module()
    raw = module.qualification2_untrusted_failure_bytes(
        stage="environment", error_code="environment-failed", cleanup=True
    )
    document = module.read_qualification2_result(raw)
    assert document["status"] == "failed"
    assert document["cleanup"] is True
    with pytest.raises(module.QualificationError):
        module.read_qualification2_result(raw, require_passed=True)


def test_wslg_display_requires_both_display_variables_and_mount(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import scripts.run_platform_qualification as runner

    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.delenv("DISPLAY", raising=False)
    assert runner._wslg_display_available() is False
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.setenv("DISPLAY", ":0")
    original_is_dir = Path.is_dir
    monkeypatch.setattr(
        Path,
        "is_dir",
        lambda path: True if str(path) == "/mnt/wslg" else original_is_dir(path),
    )
    assert runner._wslg_display_available() is True
    monkeypatch.delenv("DISPLAY")
    assert runner._wslg_display_available() is False


def test_gui_child_environment_isolates_inherited_user_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import scripts.run_platform_qualification as runner

    monkeypatch.setenv("HOME", "/inherited/home")
    monkeypatch.setenv("XDG_CONFIG_HOME", "/inherited/config")
    monkeypatch.setenv("XDG_CACHE_HOME", "/inherited/cache")
    monkeypatch.setenv("XDG_DATA_HOME", "/inherited/data")
    monkeypatch.setenv("XDG_STATE_HOME", "/inherited/state")
    monkeypatch.setenv("MPLCONFIGDIR", "/inherited/mpl")
    monkeypatch.setenv("PYTHONPATH", "/inherited/pythonpath")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/native/display-endpoint")

    environment = runner._isolated_gui_environment(tmp_path, purpose="owner")

    assert environment["HOME"] == str(tmp_path / "owner-home")
    assert environment["XDG_CONFIG_HOME"] == str(tmp_path / "owner-config")
    assert environment["XDG_CACHE_HOME"] == str(tmp_path / "owner-cache")
    assert environment["XDG_DATA_HOME"] == str(tmp_path / "owner-data")
    assert environment["XDG_STATE_HOME"] == str(tmp_path / "owner-state")
    assert environment["MPLCONFIGDIR"] == str(tmp_path / "owner-mpl")
    assert environment["PYTHONNOUSERSITE"] == "1"
    assert "PYTHONPATH" not in environment
    assert "QT_QPA_PLATFORM" not in environment
    assert environment["XDG_RUNTIME_DIR"] == "/native/display-endpoint"
    assert len(environment["GWEXPY_STUDIO_SHM_PREFIX"]) <= 14
    assert environment["GWEXPY_STUDIO_SHM_PREFIX"].isalnum()


def test_pip_report_projection_uses_a2_package_name_and_wheel_rules() -> None:
    import scripts.run_platform_qualification as runner

    projected = runner._pip_report_projection(
        {
            "install": [
                {
                    "metadata": {
                        "name": "typing-extensions",
                        "version": "4.0.0",
                    },
                    "download_info": {
                        "url": "https://example.invalid/typing_extensions-4.0.0-py3-none-any.whl",
                        "archive_info": {
                            "hashes": {"sha256": "a" * 64},
                        },
                    },
                }
            ]
        }
    )
    assert projected == [
        {
            "filename": "typing_extensions-4.0.0-py3-none-any.whl",
            "name": "typing-extensions",
            "sha256": "a" * 64,
            "version": "4.0.0",
        }
    ]


def test_legacy_summary_reader_rejects_schema2_evidence() -> None:
    module = _module()
    raw = module.qualification2_untrusted_failure_bytes(
        stage="preflight", error_code="checksum-mismatch"
    )
    with pytest.raises(module.QualificationError):
        module.qualification_campaign_summary_bytes([raw])


def _fake_schema2_kit(module, target_id: str = "ubuntu24-x86_64") -> dict[str, object]:
    target = module.trial_target(target_id)
    architecture = target.architectures[0]
    resolution_name = target.resolution_filename(architecture)
    constraints_name = target.constraints_filename(architecture)
    wheel_name = "gwexpy_studio-0.1.0-py3-none-any.whl"
    phase_one_artifacts = [
        {
            "filename": "numpy-2.5.2-py3-none-any.whl",
            "name": "numpy",
            "sha256": "8" * 64,
            "version": "2.5.2",
        },
        {
            "filename": wheel_name,
            "name": "gwexpy-studio",
            "sha256": "6" * 64,
            "version": "0.1.0",
        },
    ]
    phase_replay_artifacts = [phase_one_artifacts[0]]
    phase_one = {
        "artifacts": phase_one_artifacts,
        "conda_packages": [
            {"name": "pip", "version": "25.0.1", "build": "h"},
            {"name": "python", "version": "3.12.14", "build": "h"},
        ],
        "import_isolation": {
            "checkout_on_sys_path": False,
            "imports": ["gwexpy_studio", "gwexpy", "numpy", "PySide6"],
            "no_user_site": True,
            "python": "3.12",
            "pythonpath": False,
            "studio_visible": True,
        },
        "inspect": {
            "installed": [
                {"name": "gwexpy-studio", "version": "0.1.0"},
                {"name": "numpy", "version": "2.5.2"},
            ]
        },
        "report": {"artifacts": phase_one_artifacts},
    }
    phase_replay = {
        "artifacts": phase_replay_artifacts,
        "conda_packages": phase_one["conda_packages"],
        "import_isolation": {
            "checkout_on_sys_path": False,
            "imports": ["gwexpy", "numpy", "PySide6"],
            "no_user_site": True,
            "python": "3.12",
            "pythonpath": False,
            "studio_visible": False,
        },
        "inspect": {"installed": [{"name": "numpy", "version": "2.5.2"}]},
        "report": {"artifacts": phase_replay_artifacts},
    }
    resolution = {
        "schema": 4,
        "target_id": target_id,
        "phase_one": phase_one,
        "phase_replay": phase_replay,
        "runtime_artifacts": [{"name": "numpy", "version": "2.5.2"}],
    }
    manifest = {
        "build_id": BUILD_ID,
        "source_sha": SOURCE_SHA,
        "target_id": target_id,
        "wheel": {"filename": wheel_name, "sha256": "6" * 64},
        "archive": {"filename": "candidate.zip", "sha256": "7" * 64},
    }
    return {
        "files": {
            resolution_name: json.dumps(resolution).encode(),
            constraints_name: b"numpy==2.5.2\n",
            wheel_name: b"wheel",
        },
        "kit": Path("/tmp/qualification-kit"),
        "manifest": manifest,
        "manifest_bytes": b"kit-manifest\n",
        "scripts": {"run_trial_technical_gate.py": b"sealed"},
        "target": target,
    }


def test_schema2_lifecycle_cleans_both_prefixes_after_owner_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import scripts.run_platform_qualification as runner

    payload = _fake_schema2_kit(runner)
    events: list[str] = []
    monkeypatch.setattr(
        runner, "read_qualification_kit", lambda _kit, **_kwargs: payload
    )
    monkeypatch.setattr(
        runner,
        "validate_host",
        lambda *_args, **_kwargs: _schema2_host("ubuntu24-x86_64", "x86_64"),
    )
    monkeypatch.setattr(runner, "_conda_info", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(
        runner,
        "_fresh_conda_phase",
        lambda prefix, **_kwargs: SimpleNamespace(python=prefix / "bin/python"),
    )
    monkeypatch.setattr(runner, "_run_pip_check", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        runner, "_verify_installed_phase", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(runner, "_run", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        runner, "_runtime_python_version", lambda *_args, **_kwargs: "3.12.14"
    )
    monkeypatch.setattr(
        runner, "_probe_qt_backend", lambda *_args, **_kwargs: "wayland"
    )
    monkeypatch.setattr(
        runner,
        "_physical_qt_probe",
        lambda *_args, **_kwargs: {
            name: True
            for name in runner.required_schema2_automated_checks("ubuntu24-x86_64")
        },
    )
    monkeypatch.setattr(runner, "_unicode_space_path_roundtrip", lambda *_args: True)
    monkeypatch.setattr(runner, "_windows_path_roundtrip", lambda *_args: True)
    monkeypatch.setattr(
        runner,
        "run_installed_technical_gate",
        lambda **_kwargs: {
            "architecture": "x86_64",
            "python_version": "3.12.14",
            "installed": {"build_id": BUILD_ID, "source_sha": SOURCE_SHA},
        },
    )
    monkeypatch.setattr(
        runner,
        "_cleanup_prefix",
        lambda prefix: events.append(f"cleanup:{prefix.name}"),
    )

    def owner(
        *_args: object, answers: dict[str, bool | None], **_kwargs: object
    ) -> None:
        events.append("automatic")
        answers.update(
            {
                name: True
                for name in runner.required_schema2_owner_confirmations(
                    "ubuntu24-x86_64"
                )
            }
        )

    monkeypatch.setattr(runner, "_owner_session", owner)
    result = runner.run_qualification(
        target_id="ubuntu24-x86_64",
        kit=tmp_path / "kit",
        output_directory=tmp_path / "results",
        work_root=tmp_path / "work",
        owner_reader=lambda _prompt: "y",
    )
    module = _module()
    parsed = module.read_qualification2_result(result.read_bytes())
    assert parsed["status"] == "passed"
    assert events[0] == "automatic"
    assert events[-2:] == ["cleanup:runtime", "cleanup:replay"]


def test_schema2_lifecycle_interrupt_is_failed_and_cleanup_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import scripts.run_platform_qualification as runner

    payload = _fake_schema2_kit(runner)
    events: list[str] = []
    monkeypatch.setattr(
        runner, "read_qualification_kit", lambda _kit, **_kwargs: payload
    )
    monkeypatch.setattr(
        runner,
        "validate_host",
        lambda *_args, **_kwargs: _schema2_host("ubuntu24-x86_64", "x86_64"),
    )
    monkeypatch.setattr(runner, "_conda_info", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(
        runner,
        "_fresh_conda_phase",
        lambda prefix, **_kwargs: SimpleNamespace(python=prefix / "bin/python"),
    )
    monkeypatch.setattr(runner, "_run_pip_check", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        runner, "_verify_installed_phase", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(runner, "_run", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        runner, "_runtime_python_version", lambda *_args, **_kwargs: "3.12.14"
    )
    monkeypatch.setattr(
        runner, "_probe_qt_backend", lambda *_args, **_kwargs: "wayland"
    )
    monkeypatch.setattr(
        runner,
        "_physical_qt_probe",
        lambda *_args, **_kwargs: {
            name: True
            for name in runner.required_schema2_automated_checks("ubuntu24-x86_64")
        },
    )
    monkeypatch.setattr(runner, "_unicode_space_path_roundtrip", lambda *_args: True)
    monkeypatch.setattr(runner, "_windows_path_roundtrip", lambda *_args: True)
    monkeypatch.setattr(
        runner,
        "_cleanup_prefix",
        lambda prefix: events.append(f"cleanup:{prefix.name}"),
    )

    def interrupt(*_args: object, **_kwargs: object) -> None:
        events.append("automatic")
        raise KeyboardInterrupt

    monkeypatch.setattr(runner, "run_installed_technical_gate", interrupt)

    result = runner.run_qualification(
        target_id="ubuntu24-x86_64",
        kit=tmp_path / "kit",
        output_directory=tmp_path / "results",
        work_root=tmp_path / "work",
        owner_reader=lambda _prompt: "y",
    )
    parsed = _module().read_qualification2_result(result.read_bytes())
    assert parsed["status"] == "failed"
    assert parsed["error_code"] == "interrupted"
    assert parsed["cleanup"] is True
    assert events[-2:] == ["cleanup:runtime", "cleanup:replay"]
