"""Behavior checks for the executable Debian quick-start snippets."""

from __future__ import annotations

import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

GUIDE_PATH = Path(__file__).parents[4] / "docs" / "trial" / "debian" / "Quick-Start.md"
JAPANESE_GUIDE_PATH = GUIDE_PATH.with_name("Quick-Start.ja.md")


def _bash_blocks(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    return re.findall(r"```bash\n(.*?)```", text, flags=re.DOTALL)


def _block_containing(path: Path, fragment: str) -> str:
    for block in _bash_blocks(path):
        if fragment in block:
            return block
    raise AssertionError(f"no bash block contains {fragment!r}")


def _run(
    script: str, *, cwd: Path, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", script],
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


@pytest.mark.parametrize("guide", [GUIDE_PATH, JAPANESE_GUIDE_PATH])
@pytest.mark.parametrize(
    ("os_release", "machine", "display", "kernel", "expected_success"),
    [
        (
            "ID=ubuntu\nVERSION_ID=24.04\nPRETTY_NAME=Ubuntu",
            "x86_64",
            ":0",
            "6.1",
            False,
        ),
        ("ID=debian\nVERSION_ID=12\nPRETTY_NAME=Debian", "x86_64", ":0", "6.1", False),
        ("ID=debian\nVERSION_ID=13\nPRETTY_NAME=Debian", "aarch64", ":0", "6.1", False),
        (
            "ID=debian\nVERSION_ID=13\nPRETTY_NAME=Debian",
            "x86_64",
            ":0",
            "microsoft-standard-WSL2",
            False,
        ),
        ("ID=debian\nVERSION_ID=13\nPRETTY_NAME=Debian", "x86_64", ":0", "6.1", True),
    ],
)
def test_platform_gate_rejects_non_native_debian13_x86(
    tmp_path: Path,
    guide: Path,
    os_release: str,
    machine: str,
    display: str,
    kernel: str,
    expected_success: bool,
) -> None:
    platform_block = _block_containing(guide, "WSL is not supported")
    fake_proc = tmp_path / "kernel-osrelease"
    fake_os_release = tmp_path / "os-release"
    fake_proc.write_text(kernel, encoding="utf-8")
    fake_os_release.write_text(os_release, encoding="utf-8")
    script = platform_block.replace(
        "/proc/sys/kernel/osrelease", shlex.quote(str(fake_proc))
    ).replace("/etc/os-release", shlex.quote(str(fake_os_release)))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "uname").write_text(
        f"#!/bin/sh\nprintf '%s\\n' {machine!r}\n", encoding="utf-8"
    )
    (bin_dir / "uname").chmod(0o755)
    result = _run(
        script,
        cwd=tmp_path,
        env={"PATH": f"{bin_dir}:/usr/bin:/bin", "DISPLAY": display},
    )
    assert (result.returncode == 0) is expected_success


@pytest.mark.parametrize("guide", [GUIDE_PATH, JAPANESE_GUIDE_PATH])
def test_checksum_failure_stops_before_unzip(
    tmp_path: Path, guide: Path
) -> None:
    block = _block_containing(guide, "sha256sum -c")
    (tmp_path / "gwexpy-studio-trial-debian13-x86_64-test.zip").write_bytes(
        b"zip"
    )
    (tmp_path / "gwexpy-studio-trial-debian13-x86_64-test.zip.sha256").write_text(
        "bad  gwexpy-studio-trial-debian13-x86_64-test.zip\n", encoding="utf-8"
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "sha256sum").write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    (bin_dir / "unzip").write_text(
        "#!/bin/sh\nprintf 'called\\n' >> "
        f"{shlex.quote(str(tmp_path / 'unzip-called'))}\nexit 0\n",
        encoding="utf-8",
    )
    for command in ("sha256sum", "unzip"):
        (bin_dir / command).chmod(0o755)
    result = _run(
        block,
        cwd=tmp_path,
        env={"PATH": f"{bin_dir}:/usr/bin:/bin"},
    )
    assert result.returncode != 0
    assert not (tmp_path / "unzip-called").exists()


@pytest.mark.parametrize("conda_failure", ["list-error", "existing"])
@pytest.mark.parametrize("guide", [GUIDE_PATH, JAPANESE_GUIDE_PATH])
def test_conda_preflight_stops_before_create(
    tmp_path: Path, guide: Path, conda_failure: str
) -> None:
    block = _block_containing(guide, "conda env list")
    log = tmp_path / "conda.log"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    if conda_failure == "list-error":
        conda_body = "exit 1"
    else:
        conda_body = "printf 'gwexpy-studio-debian13 *\\n'"
    (bin_dir / "conda").write_text(
        "#!/bin/sh\nprintf '%s\\n' \"$*\" >> "
        f"{shlex.quote(str(log))}\n"
        f"if [ \"$1 $2\" = 'env list' ]; then {conda_body}; exit "
        f"{'1' if conda_failure == 'list-error' else '0'}; fi\n"
        "if [ \"$1\" = create ]; then printf 'create\\n' >> "
        f"{shlex.quote(str(log))}; fi\n",
        encoding="utf-8",
    )
    (bin_dir / "conda").chmod(0o755)
    result = _run(
        block,
        cwd=tmp_path,
        env={"PATH": f"{bin_dir}:/usr/bin:/bin"},
    )
    assert result.returncode != 0
    assert result.returncode != 0
    assert log.read_text(encoding="utf-8").splitlines() == ["env list"]


@pytest.mark.parametrize("guide", [GUIDE_PATH, JAPANESE_GUIDE_PATH])
def test_python_cpu_mismatch_stops_before_pip(tmp_path: Path, guide: Path) -> None:
    environment_block = _block_containing(guide, "conda env list")
    install_block = _block_containing(guide, "python -m pip install")
    log = tmp_path / "commands.log"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    sitecustomize = tmp_path / "sitecustomize.py"
    sitecustomize.write_text(
        "import platform\nplatform.machine = lambda: 'aarch64'\n",
        encoding="utf-8",
    )
    (bin_dir / "python").write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = - ]; then\n"
        f"  PYTHONPATH={shlex.quote(str(sitecustomize.parent))} "
        f"exec {shlex.quote(sys.executable)} -\n"
        "fi\n"
        "printf 'python %s\\n' \"$*\" >> "
        f"{shlex.quote(str(log))}\nexit 0\n",
        encoding="utf-8",
    )
    (bin_dir / "python").chmod(0o755)
    conda_function = f"""
conda() {{
  printf 'conda %s\\n' "$*" >> {shlex.quote(str(log))}
  case "$1" in
    'env') printf 'no environments\\n';;
    'create') return 0;;
    'activate') return 0;;
  esac
}}
"""
    download_block = _block_containing(guide, "archives=(gwexpy-studio-trial")
    fail_fast = download_block.splitlines()[0] + "\n"
    result = _run(
        f"{fail_fast}{conda_function}\n{environment_block}\n{install_block}",
        cwd=tmp_path,
        env={
            "PATH": f"{bin_dir}:/usr/bin:/bin",
            "PYTHONPATH": "should-be-cleared",
        },
    )
    assert result.returncode != 0
    assert "AssertionError" in result.stderr
    lines = log.read_text(encoding="utf-8").splitlines()
    assert lines[:3] == [
        "conda env list",
        "conda create -n gwexpy-studio-debian13 python=3.12 pip",
        "conda activate gwexpy-studio-debian13",
    ]
    assert not any("-m pip" in line for line in lines)
