# GWexpy Studio WSL2 trial quick start

This guide applies only to the `wsl2-ubuntu24` prerelease: Windows 11 with an
Ubuntu 24.04 WSL2 guest and WSLg. Both x86_64 and aarch64 are supported by this
bundle. Do not use it on native Ubuntu or macOS.

This human trial must be your first manual use of GWexpy Studio. Install conda
before starting. Git, a source checkout, and an editable install are not needed.

Record environment setup and installation time separately from the workflow
time. Start the workflow timer when the Welcome screen appears.

## Download and verify

Download the ZIP and matching `.zip.sha256` file from the single prerelease link
into an empty directory. Then run:

```bash
set -euo pipefail
shopt -s nullglob
sidecars=(gwexpy-studio-trial-wsl2-ubuntu24-*.zip.sha256)
test "${#sidecars[@]}" -eq 1
sha256sum -c "${sidecars[0]}"
archive="${sidecars[0]%.sha256}"
unzip "$archive"
bundle="${archive%.zip}"
cd "$bundle"
sha256sum -c SHA256SUMS
```

Every checksum must report `OK`. Stop without installing if a check fails.

## Confirm Windows, WSL2, and Ubuntu

```bash
set -euo pipefail
grep -qi microsoft /proc/sys/kernel/osrelease
test -d /mnt/wslg
grep '^ID=ubuntu$' /etc/os-release
grep '^VERSION_ID="24.04"$' /etc/os-release
architecture="$(uname -m)"
case "$architecture" in x86_64|aarch64) ;; *) exit 1 ;; esac
powershell.exe -NoLogo -NoProfile -NonInteractive -Command \
  '$env:PROCESSOR_ARCHITECTURE; [Environment]::OSVersion.Version'
```

Stop and report the output if a command fails or if the Windows architecture
does not correspond to the WSL guest architecture (`AMD64` to `x86_64`, or
`ARM64` to `aarch64`).

## Create an isolated environment

```bash
conda create -n gwexpy-studio-wsl2 python=3.12
conda activate gwexpy-studio-wsl2
export PYTHONNOUSERSITE=1
unset PYTHONPATH
python --version
conda --version
pip --version
```

Keep the environment active and repeat the two environment settings in every
new terminal used for the trial.

## Check Qt GL and install

```bash
python - <<'PY'
import ctypes

for library in ("libEGL.so.1", "libGL.so.1"):
    ctypes.CDLL(library)
print("Qt GL runtime: OK")
PY
```

If the preflight fails, install the documented Ubuntu runtime and repeat it:

```bash
sudo apt-get update
sudo apt-get install --no-install-recommends -y libegl1 libgl1
```

Install only with the constraints for the detected architecture:

```bash
architecture="$(uname -m)"
pip install --only-binary=:all: \
  -c "constraints-ubuntu24-${architecture}.txt" \
  ./gwexpy_studio-*.whl
```

Do not switch constraints, build from source, or try a workaround if installation
or launch fails.

## Run the workflow

```bash
gwexpy-studio
```

From the Welcome screen, complete **Try Sample → Load → Crop → ASD → Save →
Close → Open**. If Review or Restore appears, record whether you can continue
from it. Open About and record its Build ID.

If launch, project reopen, or recovery cannot continue, do not try a workaround.
Record the step, screen, and terminal output, then stop.

Complete `Feedback.ja.md` and reply to the email or chat that supplied the
prerelease link. Do not send confidential measurement data, credentials,
usernames, hostnames, paths, or unrelated logs.
