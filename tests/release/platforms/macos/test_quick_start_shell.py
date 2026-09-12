"""Regression checks for the macOS trial Quick Start shell recipes."""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
QUICK_STARTS = (
    REPOSITORY_ROOT / "docs" / "trial" / "macos" / "Quick-Start.md",
    REPOSITORY_ROOT / "docs" / "trial" / "macos" / "Quick-Start.ja.md",
)


def _fenced_blocks(path: Path, language: str) -> list[str]:
    text = path.read_text(encoding="utf-8")
    return re.findall(rf"```{language}\n(.*?)```", text, flags=re.DOTALL)


def _documented_block(
    marker: str, language: str = "bash", guide: Path = QUICK_STARTS[0]
) -> str:
    blocks = _fenced_blocks(guide, language)
    matching = [block for block in blocks if marker in block]
    assert len(matching) == 1
    return matching[0]


def _documented_shell_state(guide: Path) -> str:
    block = _documented_block("set -e\nif ! source", guide=guide)
    return block.split('test "$(uname -m)"', maxsplit=1)[0]


def _run(
    script: str, *, env: dict[str, str], cwd: Path
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/bin/bash", "-c", script],
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def _write_executable(path: Path, contents: str) -> None:
    path.write_text(contents, encoding="utf-8")
    path.chmod(0o755)


def _base_environment(bin_dir: Path) -> dict[str, str]:
    environment = os.environ.copy()
    environment["PATH"] = f"{bin_dir}:{environment['PATH']}"
    environment.pop("PYTHONPATH", None)
    return environment


@pytest.mark.parametrize(
    "guide", QUICK_STARTS, ids=[path.stem for path in QUICK_STARTS]
)
def test_conda_failure_stops_before_child_bash_and_install(
    tmp_path: Path, guide: Path
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    marker = tmp_path / "child-started"
    install_marker = tmp_path / "install-started"
    _write_executable(
        bin_dir / "conda",
        "#!/bin/bash\nexit 17\n",
    )
    child_command = shlex.quote(
        f'source "$CONDA_SH"; printf "%s\\n" child-base > '
        f"{shlex.quote(str(tmp_path / 'child-base'))}"
    )
    _write_executable(
        bin_dir / "bash",
        "#!/bin/bash\n"
        f"printf '%s\\n' child >> {shlex.quote(str(marker))}\n"
        f"exec /bin/bash --noprofile --norc -c {child_command}\n",
    )
    environment = _base_environment(bin_dir)

    original_shell = _documented_block(
        "CONDA_BASE=", language="zsh", guide=guide
    ).replace("/bin/bash --noprofile --norc", "bash --noprofile --norc")
    result = _run(original_shell, env=environment, cwd=tmp_path)

    assert result.returncode != 0
    assert not marker.exists()
    assert "conda info --base failed" in result.stderr

    conda_sh = tmp_path / "conda.sh"
    conda_sh.write_text("return 23\n", encoding="utf-8")
    install = _documented_shell_state(guide) + _documented_block(
        "env_name=gwexpy-studio-macos", guide=guide
    )
    _write_executable(
        bin_dir / "conda",
        "#!/bin/bash\n"
        f"printf '%s\\n' conda >> {shlex.quote(str(install_marker))}\n"
        "exit 0\n",
    )
    environment.update(
        {
            "CONDA_SH": str(conda_sh),
            "CONDA_EXE": str(bin_dir / "conda"),
            "CONDA_BASE": str(tmp_path / "conda base"),
        }
    )
    result = _run(install, env=environment, cwd=tmp_path)

    assert result.returncode != 0
    assert "conda initialization failed" in result.stderr
    assert not install_marker.exists()
    assert not install_marker.exists()


@pytest.mark.parametrize(
    "guide", QUICK_STARTS, ids=[path.stem for path in QUICK_STARTS]
)
def test_conda_base_with_spaces_reaches_child_bash_and_initializes(
    tmp_path: Path, guide: Path
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    conda_base = tmp_path / "Miniforge Base With Spaces"
    conda_sh = conda_base / "etc" / "profile.d" / "conda.sh"
    conda_sh.parent.mkdir(parents=True)
    initialized = shlex.quote(str(tmp_path / "initialized"))
    conda_sh.write_text(
        f'printf "%s\\n" "$CONDA_BASE|$CONDA_EXE|$CONDA_SH" > {initialized}\n',
        encoding="utf-8",
    )
    conda_exe = conda_base / "bin" / "conda"
    conda_exe.parent.mkdir(parents=True)
    base_literal = shlex.quote(str(conda_base))
    _write_executable(
        conda_exe,
        "#!/bin/bash\n"
        f"if [[ $1 == info && $2 == --base ]]; then printf '%s\\n' "
        f"{base_literal}; fi\n",
    )
    child_base = shlex.quote(str(tmp_path / "child-base"))
    _write_executable(
        bin_dir / "bash",
        "#!/bin/bash\n"
        f"printf '%s\\n' child >> {shlex.quote(str(tmp_path / 'child-started'))}\n"
        "exec /bin/bash --noprofile --norc -c "
        + shlex.quote(
            f'source "$CONDA_SH"\nprintf "%s\\n" "$CONDA_BASE" > {child_base}'
        )
        + "\n",
    )
    environment = _base_environment(bin_dir)
    environment["PATH"] = f"{conda_exe.parent}:{bin_dir}:{environment['PATH']}"

    result = _run(
        _documented_block("CONDA_BASE=", language="zsh", guide=guide).replace(
            "/bin/bash --noprofile --norc", "bash --noprofile --norc"
        ),
        env=environment,
        cwd=tmp_path,
    )

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "initialized").read_text(encoding="utf-8").strip() == (
        f"{conda_base}|{conda_exe}|{conda_sh}"
    )
    assert (tmp_path / "child-base").read_text(encoding="utf-8").strip() == str(
        conda_base
    )


@pytest.mark.parametrize("conda_mode", ["list-fails", "already-exists"])
@pytest.mark.parametrize(
    "guide", QUICK_STARTS, ids=[path.stem for path in QUICK_STARTS]
)
def test_install_stops_before_create_when_environment_check_fails(
    tmp_path: Path, conda_mode: str, guide: Path
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "conda-calls"
    conda = bin_dir / "conda"
    existing_output = "gwexpy-studio-macos  /tmp/existing\n"
    mode = (
        "exit 19"
        if conda_mode == "list-fails"
        else f"printf '%s' {shlex.quote(existing_output)}"
    )
    _write_executable(
        conda,
        f"#!/bin/bash\nprintf '%s\\n' \"$*\" >> {shlex.quote(str(calls))}\n"
        "if [[ $1 == env && $2 == list ]]; then " + mode + "; fi\n"
        "if [[ $1 == create ]]; then exit 99; fi\n",
    )
    environment = _base_environment(bin_dir)
    environment.update({"CONDA_EXE": str(conda), "CONDA_SH": str(tmp_path / "unused")})

    result = _run(
        _documented_block("env_name=gwexpy-studio-macos", guide=guide),
        env=environment,
        cwd=tmp_path,
    )

    assert result.returncode != 0
    assert "create" not in calls.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("reported_machine", "pip_expected"),
    [("x86_64", False), ("arm64", True)],
)
@pytest.mark.parametrize(
    "guide", QUICK_STARTS, ids=[path.stem for path in QUICK_STARTS]
)
def test_python_cpu_guard_controls_pip_install(
    tmp_path: Path, reported_machine: str, pip_expected: bool, guide: Path
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "python-calls"
    real_python = sys.executable
    site = tmp_path / "site"
    site.mkdir()
    (site / "sitecustomize.py").write_text(
        "import platform\nplatform.machine = lambda: " + repr(reported_machine) + "\n",
        encoding="utf-8",
    )
    (tmp_path / "gwexpy_studio-0.0.0-py3-none-any.whl").write_bytes(b"fixture")
    (tmp_path / "constraints-macos15-arm64.txt").write_text(
        "# fixture constraints\n", encoding="utf-8"
    )
    _write_executable(
        bin_dir / "python",
        "#!/bin/bash\n"
        f"printf '%s\\n' \"$*\" >> {shlex.quote(str(calls))}\n"
        "if [[ $1 == -m && $2 == pip && $3 == install ]]; then "
        "exit 0; "
        "fi\n"
        "if [[ $1 == -m && $2 == pip ]]; then exit 0; "
        "fi\n"
        f'PYTHONPATH={shlex.quote(str(site))} exec {shlex.quote(real_python)} "$@"\n',
    )
    conda_calls = tmp_path / "conda-calls"
    _write_executable(
        bin_dir / "conda",
        "#!/bin/bash\n"
        f"printf '%s\\n' \"$*\" >> {shlex.quote(str(conda_calls))}\n"
        "if [[ $1 == env && $2 == list ]]; then exit 0; fi\n"
        "if [[ $1 == create || $1 == activate || $1 == --version ]]; then exit 0; fi\n",
    )
    conda_sh = tmp_path / "conda.sh"
    conda_sh.write_text("# no-op fixture\n", encoding="utf-8")
    environment = _base_environment(bin_dir)
    environment.update({"CONDA_SH": str(conda_sh)})
    script = _documented_shell_state(guide) + _documented_block(
        "env_name=gwexpy-studio-macos", guide=guide
    )
    result = _run(script, env=environment, cwd=tmp_path)

    if reported_machine == "x86_64":
        assert result.returncode != 0
        assert not any("-m pip" in line for line in calls.read_text().splitlines())
        assert "AssertionError" in result.stderr
    else:
        assert result.returncode == 0, result.stderr
        assert any("-m pip install" in line for line in calls.read_text().splitlines())
        assert (
            "create -n gwexpy-studio-macos python=3.12 pip" in conda_calls.read_text()
        )
