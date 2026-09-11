"""Contracts for the source-tree-independent installed-wheel technical gate."""

from __future__ import annotations

import importlib
import inspect
import json
import signal
import sys
from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.unit


def _gate():
    return importlib.import_module("scripts.run_trial_technical_gate")


def _identity() -> dict[str, str]:
    return {
        "architecture": "x86_64",
        "build_id": "P-abcdef0-20260907-r1-a1",
        "python_version": "3.12.12",
        "source_sha": "abcdef0123456789abcdef0123456789abcdef01",
        "version": "0.1.0a1+trial.p.gabcdef0.20260907.r1.a1",
    }


def test_gate_qapplication_arguments_use_a_concrete_list() -> None:
    """Qt 6.11 must receive a concrete list rather than a tuple proxy."""
    arguments = _gate()._qapplication_arguments()

    assert type(arguments) is list
    assert arguments == [sys.argv[0]]


def test_gate_stage_record_is_canonical_and_path_free(tmp_path: Path) -> None:
    gate = _gate()

    gate._record_gate_stage(tmp_path, "producer", "crop-dialog")

    stage_path = tmp_path / "technical-gate-stage.json"
    assert stage_path.read_bytes() == (
        b'{"phase":"producer","stage":"crop-dialog"}\n'
    )
    assert gate.read_gate_stage(tmp_path) == "producer/crop-dialog"
    with pytest.raises(gate.GateError, match="stage"):
        gate._record_gate_stage(tmp_path, "producer", "not/a-stage")


def test_gate_waits_for_sample_catalog_before_requesting_inspection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Try Sample cannot inspect until its asynchronous format catalog is ready."""
    gate = _gate()
    idle = object()
    bridge = SimpleNamespace(state=object())
    inspect_button = SimpleNamespace(enabled=False)
    inspect_button.isEnabled = lambda: inspect_button.enabled
    window = SimpleNamespace(bridge=bridge)
    panel = SimpleNamespace(inspect_button=inspect_button)

    def assert_catalog_predicate(
        _app: object, predicate: object, label: str, timeout_s: float = 30.0
    ) -> None:
        del timeout_s
        assert label == "sample catalog"
        assert not predicate()  # type: ignore[operator]
        bridge.state = idle
        assert not predicate()  # type: ignore[operator]
        inspect_button.enabled = True
        assert predicate()  # type: ignore[operator]

    monkeypatch.setattr(gate, "_wait", assert_catalog_predicate)

    gate._wait_for_sample_catalog(
        app=object(), window=window, panel=panel, idle_state=idle
    )


def test_gate_selects_a_timeseries_before_the_post_asd_crop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The recovery-changing Crop must not target the ASD FrequencySeries."""
    gate = _gate()
    idle = object()
    bridge = SimpleNamespace(state=idle)
    crop_action = SimpleNamespace(enabled=False)
    crop_action.isEnabled = lambda: crop_action.enabled
    selected: list[str] = []

    class Tree:
        def setCurrentItem(self, item: object) -> None:  # noqa: N802 - Qt API spelling
            selected.append(item.object_id)  # type: ignore[attr-defined]
            window._current_object_id = item.object_id  # type: ignore[attr-defined]
            crop_action.enabled = True

    objects = (
        SimpleNamespace(object_id="read", kind="TimeSeries"),
        SimpleNamespace(object_id="crop", kind="TimeSeries"),
        SimpleNamespace(object_id="asd", kind="FrequencySeries"),
    )
    window = SimpleNamespace(
        bridge=bridge,
        crop_action=crop_action,
        project=SimpleNamespace(objects=objects),
        source_tree=Tree(),
        _current_object_id="asd",
    )
    item = SimpleNamespace(object_id="crop")
    window._find_object_item = lambda _tree, object_id: (
        item if object_id == "crop" else None
    )

    def assert_selection_predicate(
        _app: object, predicate: object, label: str, timeout_s: float = 30.0
    ) -> None:
        del timeout_s
        assert label == "post-ASD Crop input"
        assert predicate()  # type: ignore[operator]

    monkeypatch.setattr(gate, "_wait", assert_selection_predicate)

    assert (
        gate._select_latest_timeseries_for_crop(
            app=object(), window=window, idle_state=idle
        )
        == "crop"
    )
    assert selected == ["crop"]


def test_installed_package_must_be_under_site_packages_and_outside_checkout(
    tmp_path: Path,
) -> None:
    """A gate fails closed if Studio resolves from P rather than the fresh venv."""
    checkout = tmp_path / "public-p"
    checkout_module = checkout / "src" / "gwexpy_studio" / "__init__.py"
    checkout_module.parent.mkdir(parents=True)
    checkout_module.write_text("", encoding="utf-8")
    purelib = tmp_path / "venv" / "lib" / "python3.12" / "site-packages"
    installed_module = purelib / "gwexpy_studio" / "__init__.py"
    installed_module.parent.mkdir(parents=True)
    installed_module.write_text("", encoding="utf-8")

    _gate().verify_installed_module_path(
        module_path=installed_module,
        site_roots=(purelib,),
        checkout_root=checkout,
    )
    with pytest.raises(_gate().GateError, match="checkout"):
        _gate().verify_installed_module_path(
            module_path=checkout_module,
            site_roots=(purelib,),
            checkout_root=checkout,
        )


def test_installed_package_ignores_absent_site_package_candidates(
    tmp_path: Path,
) -> None:
    """Ubuntu's venv may report absent dist-packages roots alongside purelib."""
    checkout = tmp_path / "public-p"
    checkout.mkdir()
    purelib = tmp_path / "venv" / "lib" / "python3.12" / "site-packages"
    installed_module = purelib / "gwexpy_studio" / "__init__.py"
    installed_module.parent.mkdir(parents=True)
    installed_module.write_text("", encoding="utf-8")
    absent_site_roots = (
        tmp_path / "venv" / "local" / "lib" / "python3.12" / "dist-packages",
        tmp_path / "venv" / "lib" / "python3" / "dist-packages",
        tmp_path / "venv" / "lib" / "python3.12" / "dist-packages",
    )

    _gate().verify_installed_module_path(
        module_path=installed_module,
        site_roots=(purelib, *absent_site_roots),
        checkout_root=checkout,
    )


def test_installed_package_rejects_only_absent_site_package_candidates(
    tmp_path: Path,
) -> None:
    """Skipping Ubuntu's absent candidates must not permit an unrooted import."""
    checkout = tmp_path / "public-p"
    checkout.mkdir()
    foreign_module = tmp_path / "foreign" / "gwexpy_studio" / "__init__.py"
    foreign_module.parent.mkdir(parents=True)
    foreign_module.write_text("", encoding="utf-8")
    absent_site_roots = (
        tmp_path / "venv" / "local" / "lib" / "python3.12" / "dist-packages",
        tmp_path / "venv" / "lib" / "python3" / "dist-packages",
        tmp_path / "venv" / "lib" / "python3.12" / "dist-packages",
    )

    with pytest.raises(_gate().GateError, match="site-packages"):
        _gate().verify_installed_module_path(
            module_path=foreign_module,
            site_roots=absent_site_roots,
            checkout_root=checkout,
        )


def test_gate_environment_isolated_and_result_is_bounded_path_free(
    tmp_path: Path,
) -> None:
    """The external Qt gate has its own roots and never emits host paths."""
    inherited = {
        "GWEXPY_STUDIO_IO_CAPABILITIES": "/private/forged-policy.json",
        "HOME": "/private/home",
        "PYTHONPATH": "/private/checkout/src",
        "XDG_CACHE_HOME": "/old/cache",
    }

    environment = _gate().gate_environment(tmp_path, inherited, "trialgate-")
    encoded = (
        _gate()
        .gate_result_json(
            {
                "asd": True,
                "crop": True,
                "launcher_import": True,
                "project_reopen": True,
                "recovery": True,
                "save_project": True,
                "shared_memory_cleanup": True,
                "try_sample": True,
                "welcome": True,
                "worker_exit": True,
            },
            installed=_identity(),
        )
        .decode("utf-8")
    )

    assert "PYTHONPATH" not in environment
    assert "GWEXPY_STUDIO_IO_CAPABILITIES" not in environment
    assert environment["QT_QPA_PLATFORM"] == "offscreen"
    assert environment["MPLBACKEND"] == "Agg"
    assert environment["GWEXPY_STUDIO_SHM_PREFIX"] == "trialgate-"
    assert str(tmp_path) not in encoded
    assert "/private" not in encoded
    result = json.loads(encoded)
    assert result == {
        "architecture": "x86_64",
        "checks": {
            "asd": True,
            "crop": True,
            "launcher_import": True,
            "project_reopen": True,
            "recovery": True,
            "save_project": True,
            "shared_memory_cleanup": True,
            "try_sample": True,
            "welcome": True,
            "worker_exit": True,
        },
        "installed": {
            "build_id": _identity()["build_id"],
            "source_sha": _identity()["source_sha"],
            "version": _identity()["version"],
        },
        "python_version": "3.12.12",
        "schema": 2,
        "status": "passed",
    }


def test_macos_gate_environment_keeps_native_cocoa_selection(tmp_path: Path) -> None:
    environment = _gate().gate_environment(
        tmp_path, {"QT_QPA_PLATFORM": "forged"}, "trialgate-", native_qt=True
    )

    assert "QT_QPA_PLATFORM" not in environment


def test_operation_dialog_acceptance_is_native_safe_and_bounded() -> None:
    """Cocoa dialogs use a direct button signal and cannot poll forever."""
    source = inspect.getsource(_gate()._click_dialog)

    assert "button.click()" in source
    assert "QTest.mouseClick" not in source
    assert "deadline_timer" in source
    assert "topLevelWidgets" in source
    assert "technical-gate operation dialog timed out" in source


def test_recovery_message_finds_the_visible_cocoa_dialog() -> None:
    """Cocoa may expose a visible message box without an active modal widget."""
    from PySide6.QtWidgets import QApplication, QMessageBox, QWidget

    app = QApplication.instance() or QApplication([])
    owner = QWidget()
    dialog = QMessageBox(owner)
    dialog.setWindowTitle("Recover unfinished work")
    dialog.addButton("Restore", QMessageBox.ButtonRole.AcceptRole)
    dialog.show()
    app.processEvents()
    cocoa_like_app = SimpleNamespace(
        activeModalWidget=lambda: None,
        topLevelWidgets=lambda: [dialog],
    )

    scheduled = _gate()._schedule_message_box_button(
        app=cocoa_like_app,
        owner=owner,
        label="Restore",
        title="Recover unfinished work",
        required=True,
        timeout_s=0.2,
    )
    _gate()._wait(
        app,
        lambda: scheduled.handled or scheduled.error is not None,
        "recovery message click",
        timeout_s=1.0,
    )

    assert scheduled.error is None
    assert scheduled.handled is True
    dialog.close()
    owner.close()


def test_recovery_message_reports_visible_and_selected_boundaries() -> None:
    """A diagnostic stage can distinguish discovery from button activation."""
    from PySide6.QtWidgets import QApplication, QMessageBox, QWidget

    app = QApplication.instance() or QApplication([])
    owner = QWidget()
    dialog = QMessageBox(owner)
    dialog.setWindowTitle("Recover unfinished work")
    dialog.addButton("Restore", QMessageBox.ButtonRole.AcceptRole)
    dialog.show()
    app.processEvents()
    cocoa_like_app = SimpleNamespace(
        activeModalWidget=lambda: None,
        topLevelWidgets=lambda: [dialog],
    )
    boundaries: list[str] = []

    scheduled = _gate()._schedule_message_box_button(
        app=cocoa_like_app,
        owner=owner,
        label="Restore",
        title="Recover unfinished work",
        required=True,
        timeout_s=0.2,
        on_visible=lambda: boundaries.append("visible"),
        on_selected=lambda: boundaries.append("selected"),
    )
    _gate()._wait(
        app,
        lambda: scheduled.handled or scheduled.error is not None,
        "recovery message diagnostics",
        timeout_s=1.0,
    )

    assert scheduled.error is None
    assert scheduled.handled is True
    assert boundaries == ["visible", "selected"]
    dialog.close()
    owner.close()


def test_consumer_recovery_diagnostics_cover_each_modal_boundary() -> None:
    source = inspect.getsource(_gate()._run_consumer_launcher)

    for stage in (
        "recovery-review-trigger",
        "recovery-review-requested",
        "recovery-candidate-visible",
        "recovery-candidate-selected",
        "unsaved-project-visible",
        "unsaved-project-discarded",
        "recovery-restored",
    ):
        assert f'"{stage}"' in source


def test_recovery_dispatch_diagnostics_preserve_accepted_command_and_result(
) -> None:
    gate = _gate()
    delegated: list[tuple[object, ...]] = []
    stages: list[str] = []
    candidates = [{"run_id": "recovery-1"}]
    result = SimpleNamespace(success=True, payload={"recoveries": candidates})

    def dispatch(
        kind: str,
        payload: dict[str, object],
        *,
        pending_action: str,
        pending_params: dict[str, object] | None = None,
    ) -> bool:
        delegated.append((kind, payload, pending_action, pending_params))
        return True

    def handle_workspace_result(value: object) -> str:
        delegated.append(("result", value))
        return "handled"

    window = SimpleNamespace(
        _dispatch_command=dispatch,
        _handle_workspace_result=handle_workspace_result,
        _pending_action="list_recoveries",
    )
    gate._instrument_recovery_boundaries(window=window, record=stages.append)

    accepted = window._dispatch_command(
        "list_recoveries",
        {"scope": "all"},
        pending_action="list_recoveries",
        pending_params={"source": "review"},
    )
    handled = window._handle_workspace_result(result)

    assert accepted is True
    assert handled == "handled"
    assert delegated == [
        (
            "list_recoveries",
            {"scope": "all"},
            "list_recoveries",
            {"source": "review"},
        ),
        ("result", result),
    ]
    assert stages == [
        "recovery-list-dispatch-requested",
        "recovery-list-dispatch-accepted",
        "recovery-list-result-received",
        "recovery-list-result-succeeded",
        "recovery-candidates-received",
    ]


def test_recovery_dispatch_diagnostics_do_not_report_rejected_command_as_accepted(
) -> None:
    gate = _gate()
    stages: list[str] = []
    window = SimpleNamespace(
        _dispatch_command=lambda *_args, **_kwargs: False,
        _handle_workspace_result=lambda result: result,
        _pending_action=None,
    )
    gate._instrument_recovery_boundaries(window=window, record=stages.append)

    accepted = window._dispatch_command(
        "list_recoveries", {}, pending_action="list_recoveries"
    )

    assert accepted is False
    assert stages == ["recovery-list-dispatch-requested"]


def test_recovery_dispatch_diagnostics_distinguish_an_empty_list_result() -> None:
    gate = _gate()
    stages: list[str] = []
    result = SimpleNamespace(success=True, payload={"recoveries": []})
    window = SimpleNamespace(
        _dispatch_command=lambda *_args, **_kwargs: True,
        _handle_workspace_result=lambda value: value,
        _pending_action="list_recoveries",
    )
    gate._instrument_recovery_boundaries(window=window, record=stages.append)

    handled = window._handle_workspace_result(result)

    assert handled is result
    assert stages == [
        "recovery-list-result-received",
        "recovery-list-result-succeeded",
    ]


def test_recovery_dispatch_diagnostics_record_an_error_result_at_handler_entry(
) -> None:
    gate = _gate()
    stages: list[str] = []
    handled: list[object] = []
    result = SimpleNamespace(success=False, payload={})

    def handle_workspace_result(value: object) -> str:
        handled.append(value)
        return "handled"

    window = SimpleNamespace(
        _dispatch_command=lambda *_args, **_kwargs: True,
        _handle_workspace_result=handle_workspace_result,
        _pending_action="list_recoveries",
    )
    gate._instrument_recovery_boundaries(window=window, record=stages.append)

    outcome = window._handle_workspace_result(result)

    assert outcome == "handled"
    assert handled == [result]
    assert stages == ["recovery-list-result-received"]


def test_consumer_installs_recovery_dispatch_diagnostics_before_review() -> None:
    source = inspect.getsource(_gate()._run_consumer_launcher)

    assert source.index("_instrument_recovery_boundaries(") < source.index(
        "QTest.mouseClick("
    )


def test_shared_memory_cleanup_probe_uses_reattach_not_dev_shm() -> None:
    assert _gate().shared_memory_cleanup_probe("trialgate-") is True


def test_gate_result_reader_requires_the_exact_bounded_success_schema() -> None:
    """The resolver accepts only a complete, internally consistent gate record."""
    checks = {name: True for name in _gate()._CHECK_NAMES}
    encoded = _gate().gate_result_json(checks, installed=_identity())

    assert _gate().read_gate_result(encoded) == {
        "architecture": "x86_64",
        "checks": checks,
        "installed": {
            "build_id": _identity()["build_id"],
            "source_sha": _identity()["source_sha"],
            "version": _identity()["version"],
        },
        "python_version": "3.12.12",
        "schema": 2,
        "status": "passed",
    }

    malformed = json.loads(encoded)
    malformed["checks"]["worker_exit"] = False
    with pytest.raises(_gate().GateError, match="status"):
        _gate().read_gate_result(json.dumps(malformed, sort_keys=True).encode("utf-8"))


def test_gate_command_uses_isolated_venv_python_and_binds_private_paths(
    tmp_path: Path,
) -> None:
    """The master invokes the immutable, source-verified gate script it receives."""
    python = tmp_path / "phase-two" / "bin" / "python"
    checkout = tmp_path / "public-p"
    work_root = tmp_path / "private-work"
    result = work_root / "technical-gate.json"
    gate_fd = 37

    command = _gate().technical_gate_command(
        python=python,
        gate_fd=gate_fd,
        checkout_root=checkout,
        work_root=work_root,
        result_path=result,
    )

    assert command[:3] == (str(python), "-I", "-c")
    assert command[3] == _gate()._SEALED_GATE_BOOTSTRAP
    assert command[4] == str(gate_fd)
    assert command[command.index("--gate-fd") + 1] == str(gate_fd)
    assert command[command.index("--checkout") + 1] == str(checkout)
    assert command[command.index("--work-root") + 1] == str(work_root)
    assert command[command.index("--result") + 1] == str(result)


def test_external_recovery_gate_runs_a_crashing_producer_then_a_fresh_consumer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recovery qualification uses two isolated phase-two launcher processes."""
    checkout = tmp_path / "public-p"
    work_root = tmp_path / "private-work"
    phase_python = tmp_path / "phase-two" / "bin" / "python"
    gate_fd = 37
    checkout.mkdir()
    work_root.mkdir()
    seen: list[tuple[str, ...]] = []

    def fake_run(command: Sequence[str], **kwargs: object) -> SimpleNamespace:
        values: tuple[str, ...] = tuple(command)
        seen.append(values)
        if "--phase" not in values:
            assert values == (str(phase_python), str(work_root / "python-export.py"))
            assert "pass_fds" not in kwargs
            return SimpleNamespace(returncode=0)
        assert kwargs["pass_fds"] == (gate_fd,)
        phase = values[values.index("--phase") + 1]
        if phase == "producer":
            (work_root / "trial.gwxproj").write_text("saved", encoding="utf-8")
            (work_root / "python-export.py").write_text(
                "print('export')\n", encoding="utf-8"
            )
        return SimpleNamespace(returncode=-signal.SIGKILL if phase == "producer" else 0)

    monkeypatch.setattr(
        _gate(), "subprocess", SimpleNamespace(run=fake_run), raising=False
    )
    checks = {name: False for name in _gate()._CHECK_NAMES}

    _gate().run_external_recovery_gate(
        checks=checks,
        checkout=checkout,
        gate_fd=gate_fd,
        phase_python=phase_python,
        work_root=work_root,
    )

    phase_commands = [command for command in seen if "--phase" in command]
    assert [command[command.index("--phase") + 1] for command in phase_commands] == [
        "producer",
        "consumer",
    ]
    assert all(
        command[:3] == (str(phase_python), "-I", "-c") for command in phase_commands
    )
    assert all(
        command[3] == _gate()._SEALED_GATE_BOOTSTRAP for command in phase_commands
    )
    assert all(command[4] == str(gate_fd) for command in phase_commands)
    assert "--project" not in phase_commands[0]
    assert phase_commands[1][phase_commands[1].index("--project") + 1] == str(
        work_root / "trial.gwxproj"
    )
    assert all(
        checks[name]
        for name in (
            "welcome",
            "try_sample",
            "crop",
            "asd",
            "save_project",
            "project_reopen",
            "recovery",
            "worker_exit",
        )
    )


def test_phase_main_runs_the_producer_without_a_final_gate_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The crash producer is a child phase, not a second master gate."""
    checkout = tmp_path / "public-p"
    work_root = tmp_path / "private-work"
    checkout.mkdir()
    work_root.mkdir()
    seen: list[tuple[Path, Path]] = []

    monkeypatch.setattr(
        _gate(),
        "_run_producer_launcher",
        lambda *, checkout, work_root: seen.append((checkout, work_root)),
        raising=False,
    )

    assert (
        _gate().main(
            [
                "--phase",
                "producer",
                "--checkout",
                str(checkout),
                "--work-root",
                str(work_root),
            ]
        )
        == 0
    )
    assert seen == [(checkout.resolve(), work_root.resolve())]


def test_external_recovery_gate_rejects_a_crashed_producer_without_saved_project(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A SIGKILL alone cannot claim that the recovery producer reached Save."""
    checkout = tmp_path / "public-p"
    work_root = tmp_path / "private-work"
    gate_fd = 37
    checkout.mkdir()
    work_root.mkdir()

    monkeypatch.setattr(
        _gate(),
        "subprocess",
        SimpleNamespace(
            run=lambda *_args, **_kwargs: SimpleNamespace(returncode=-signal.SIGKILL)
        ),
        raising=False,
    )

    with pytest.raises(_gate().GateError, match="did not save"):
        _gate().run_external_recovery_gate(
            checks={name: False for name in _gate()._CHECK_NAMES},
            checkout=checkout,
            gate_fd=gate_fd,
            phase_python=tmp_path / "phase-two" / "bin" / "python",
            work_root=work_root,
        )
