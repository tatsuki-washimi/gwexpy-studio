"""Headless contracts for release-trial runtime support."""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import subprocess
import sys
import zipfile
from importlib import resources
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

SOURCE_ROOT = Path(__file__).resolve().parents[2] / "src"


def _user_paths():
    return importlib.import_module("gwexpy_studio.runtime.user_paths")


def _trial_support():
    return importlib.import_module("gwexpy_studio.runtime.trial")


@pytest.mark.contract("TRT-001")
def test_user_paths_use_absolute_xdg_homes_without_creating_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Configured XDG roots remain a pure resolution operation."""
    configured = {
        "XDG_CONFIG_HOME": tmp_path / "config-home",
        "XDG_DATA_HOME": tmp_path / "data-home",
        "XDG_STATE_HOME": tmp_path / "state-home",
        "XDG_CACHE_HOME": tmp_path / "cache-home",
    }
    for name, directory in configured.items():
        monkeypatch.setenv(name, str(directory))

    paths = _user_paths()

    assert paths.config_directory() == configured["XDG_CONFIG_HOME"] / "gwexpy-studio"
    assert paths.data_directory() == configured["XDG_DATA_HOME"] / "gwexpy-studio"
    assert paths.state_directory() == configured["XDG_STATE_HOME"] / "gwexpy-studio"
    assert paths.cache_directory() == configured["XDG_CACHE_HOME"] / "gwexpy-studio"
    assert all(not directory.exists() for directory in configured.values())


@pytest.mark.contract("TRT-002")
def test_user_paths_ignore_relative_homes_and_use_xdg_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Relative XDG values must not escape the standard home-directory layout."""
    paths = _user_paths()
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    for variable in (
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_STATE_HOME",
        "XDG_CACHE_HOME",
    ):
        monkeypatch.setenv(variable, "relative-directory")

    assert paths.config_directory() == home / ".config" / "gwexpy-studio"
    assert paths.data_directory() == home / ".local" / "share" / "gwexpy-studio"
    assert paths.state_directory() == home / ".local" / "state" / "gwexpy-studio"
    assert paths.cache_directory() == home / ".cache" / "gwexpy-studio"
    assert not home.exists()


@pytest.mark.contract("TRT-003")
def test_bootstrap_matplotlib_sets_cache_env_without_creating_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The import-order bootstrap fixes MPLCONFIGDIR without doing I/O."""
    paths = _user_paths()
    cache_home = tmp_path / "cache-home"
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache_home))
    monkeypatch.setenv("MPLCONFIGDIR", "/not-the-studio-cache")

    expected = cache_home / "gwexpy-studio" / "matplotlib"
    assert paths.bootstrap_matplotlib() == expected
    assert os.environ["MPLCONFIGDIR"] == str(expected)
    assert not cache_home.exists()


@pytest.mark.contract("TRT-004")
def test_locking_state_directory_delegates_to_user_path_resolver(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Existing recovery and lock callers retain the central state-path contract."""
    paths = importlib.import_module("gwexpy_studio.user_paths")
    locking = importlib.import_module("gwexpy_studio.persistence.locking")
    expected = tmp_path / "central-state"
    monkeypatch.setattr(paths, "state_directory", lambda: expected)

    assert locking.state_directory() == expected


@pytest.mark.contract("TRT-005")
def test_recent_projects_are_path_only_canonical_deduplicated_and_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Recent projects retain only canonical paths, including missing projects."""
    config_home = tmp_path / "config-home"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    trial = _trial_support()
    store = trial.RecentProjectStore()
    missing = tmp_path / "projects" / "missing.gwxproj"

    store.record(missing)
    store.record(missing.parent / "." / missing.name)

    assert store.path == config_home / "gwexpy-studio" / "recent-projects.json"
    assert store.list() == (missing.resolve(),)
    assert not missing.exists()

    projects = tuple(tmp_path / f"project-{index}.gwxproj" for index in range(12))
    for project in projects:
        store.record(project)
    expected = tuple(project.resolve() for project in reversed(projects[-10:]))

    assert store.list() == expected
    assert json.loads(store.path.read_text(encoding="utf-8")) == [
        str(project) for project in expected
    ]


@pytest.mark.contract("TRT-006")
def test_recent_projects_clear_and_tolerate_invalid_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A corrupt recent-project document cannot break startup or clearing it."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config-home"))
    store = _trial_support().RecentProjectStore()
    store.path.parent.mkdir(parents=True)
    store.path.write_text("{not-json", encoding="utf-8")

    assert store.list() == ()
    store.clear()
    assert not store.path.exists()

    store.path.mkdir(parents=True)
    sentinel = store.path / "keep.txt"
    sentinel.write_text("do-not-remove", encoding="utf-8")

    assert store.list() == ()
    store.clear()
    assert store.path.is_dir()
    assert sentinel.read_text(encoding="utf-8") == "do-not-remove"


@pytest.mark.contract("TRT-007")
def test_sample_path_copies_packaged_content_and_reuses_matching_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The packaged sample is copied once into its content-addressed data path."""
    data_home = tmp_path / "data-home"
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))
    trial = _trial_support()
    source = resources.files("gwexpy_studio").joinpath(
        "assets", "alpha-timeseries.csv"
    )
    source_bytes = source.read_bytes()
    digest = hashlib.sha256(source_bytes).hexdigest()

    target = trial.sample_path()
    expected = (
        data_home
        / "gwexpy-studio"
        / "samples"
        / "0.1.0a1"
        / digest
        / "alpha-timeseries.csv"
    )
    before_mtime = target.stat().st_mtime_ns

    assert target == expected
    assert target.read_bytes() == source_bytes
    assert trial.sample_path() == target
    assert target.stat().st_mtime_ns == before_mtime


@pytest.mark.contract("TRT-008")
def test_sample_path_preserves_a_modified_normal_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A user-modified sample is retained while Studio uses a safe alternative."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data-home"))
    trial = _trial_support()
    source = resources.files("gwexpy_studio").joinpath(
        "assets", "alpha-timeseries.csv"
    )
    source_bytes = source.read_bytes()
    digest = hashlib.sha256(source_bytes).hexdigest()
    normal = trial.sample_path()
    normal.write_text("user edits", encoding="utf-8")

    alternative = trial.sample_path()

    assert normal.read_text(encoding="utf-8") == "user edits"
    assert alternative != normal
    assert alternative.name == f"alpha-timeseries-{digest}.csv"
    assert alternative.read_bytes() == source_bytes
    assert trial.sample_path() == alternative


@pytest.mark.contract("TRT-009")
def test_wheel_includes_the_packaged_alpha_sample(tmp_path: Path) -> None:
    """Setuptools exposes the sample resource outside the source checkout."""
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
    wheels = tuple(wheelhouse.glob("*.whl"))
    assert len(wheels) == 1
    with zipfile.ZipFile(wheels[0]) as archive:
        assert "gwexpy_studio/assets/alpha-timeseries.csv" in archive.namelist()


def test_normal_wheel_declares_gui_runtime_and_launcher(tmp_path: Path) -> None:
    """A participant-facing wheel installs its Qt launcher dependency by default."""
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
                name
                for name in archive.namelist()
                if name.endswith("entry_points.txt")
            )
        ).decode("utf-8")
        metadata = archive.read(
            next(name for name in archive.namelist() if name.endswith("METADATA"))
        ).decode("utf-8")

    assert "[gui_scripts]" in entry_points
    assert "gwexpy-studio = gwexpy_studio.ui.app:main" in entry_points
    assert "Requires-Dist: PySide6-Essentials==6.11.2\n" in metadata
    assert 'extra == "gui"' not in metadata


@pytest.mark.contract("TRT-010")
def test_trial_runtime_imports_without_scientific_or_qt_packages() -> None:
    """Runtime startup support remains safe before scientific or Qt imports."""
    probe = """
import sys
import gwexpy_studio.runtime.trial
import gwexpy_studio.runtime.user_paths
from gwexpy_studio.persistence.locking import state_directory

assert state_directory()
for package in (
    "numpy", "scipy", "astropy", "gwpy", "gwexpy", "PySide6", "PyQt6", "qtpy"
):
    assert package not in sys.modules, package
"""
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(SOURCE_ROOT)
    completed = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=SOURCE_ROOT.parent,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )

    assert completed.returncode == 0, completed.stderr
