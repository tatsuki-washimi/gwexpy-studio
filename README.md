# GWexpy Studio

[日本語](README.ja.md)

GWexpy Studio is a desktop GUI for inspecting scientific data, running GWexpy
analysis, saving a project, and exporting ordinary Python.

It is intended for researchers and students who use Python tools but do not
need to learn Git or begin by writing a notebook.

## Installation

**Choose your OS, download its trial ZIP, install it in a dedicated conda environment, and launch Studio.**
No Python programming, Git, source checkout, or GitHub account is required.

### Availability and downloads

**As of September 17, 2026, the new four-platform trial has not been published as a Release.**
The instructions below are prepared, but download links for the new trial are not yet available.
The old Ubuntu reference release is a separate distribution. Do not combine it with the new-trial instructions below.

| Your environment | New trial download | Instructions on this page |
|---|---|---|
| Ubuntu 24.04, x86_64, native desktop (not WSL) | Not yet published | [Ubuntu](#install-ubuntu) |
| Debian 13, x86_64, native desktop (not WSL) | Not yet published | [Debian](#install-debian) |
| Windows 11 x86_64 + WSL2 Ubuntu 24.04 + WSLg | Not yet published | [Windows / WSL2](#install-wsl2) |
| Windows 11 ARM64 + WSL2 Ubuntu 24.04 aarch64 + WSLg | Not yet published | [Windows / WSL2](#install-wsl2) |
| Apple Silicon Mac, macOS 15 or later | Not yet published | [macOS](#install-macos) |

<!--
Publication: replace only verified, published target rows with links to their exact
Release page, existing trial ZIP, and matching .zip.sha256 asset.
The two WSL2 rows use the same target ZIP; select CPU-specific constraints below.
Do not guess asset names, use /releases/latest, or fall back to the old Ubuntu release.
Update tested OS versions, date, and matching instructions together with README.ja.md.
-->

### Before you begin

You need conda in your terminal and network access for conda and pip.
The instructions create a dedicated Python 3.12 environment without changing your operating system's Python.
For WSL2, conda must be available **inside the WSL2 Ubuntu terminal**, not just on Windows.

Each OS procedure also checks the display environment.
Installing missing system libraries on Linux or WSL2 may require an administrator.

### Installation at a glance

**1. Download your OS's ZIP and `.zip.sha256` → 2. Verify and extract →
3. Install in a dedicated Python 3.12 conda environment → 4. Run `gwexpy-studio`.**

After publication, download the **trial ZIP and `.zip.sha256` from the same Release** listed above into an empty folder.
Use the trial archive, not a source-code ZIP or `Code → Download ZIP`.

Open a terminal in that folder, expand only your OS below, and run its code blocks in order.
Stop on any error. The checks are part of installation; do not run only the final `pip install` command.

<a name="install-ubuntu"></a>

### Ubuntu 24.04 x86_64

<details>
<summary>Expand installation commands on this page</summary>

Use a Bash terminal on the Ubuntu desktop, with conda, `unzip`, and `sha256sum`. For WSL2, use the Windows / WSL2 procedure instead.

**1. Verify and extract the ZIP**

```bash
set -euo pipefail
shopt -s nullglob
archives=(gwexpy-studio-trial-ubuntu24-x86_64-*.zip)
sidecars=(gwexpy-studio-trial-ubuntu24-x86_64-*.zip.sha256)
test "${#archives[@]}" -eq 1
test "${#sidecars[@]}" -eq 1
archive="${archives[0]}"
sidecar="${sidecars[0]}"
test "${archive}.sha256" = "$sidecar"
sha256sum -c "$sidecar"
unzip "$archive"
bundle="${archive%.zip}"
cd "$bundle"
sha256sum -c SHA256SUMS
```

Confirm that every checksum reports `OK`. Continue in the same terminal, now inside the extracted folder.

**2. Check the OS, CPU, and desktop**

```bash
set -euo pipefail
if grep -qi microsoft /proc/sys/kernel/osrelease; then
  echo 'WSL is not supported by ubuntu24-x86_64' >&2
  exit 1
fi
. /etc/os-release
test "${ID:-}" = ubuntu
test "${VERSION_ID:-}" = 24.04
test "$(uname -m)" = x86_64
test -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}"
printf 'ID=%s VERSION_ID=%s arch=%s\n' "$ID" "$VERSION_ID" "$(uname -m)"
```

**3. Create a dedicated Python 3.12 conda environment**

The procedure stops rather than overwriting an existing environment. For an existing installation, use “Launching again” below.

```bash
set -euo pipefail
env_name=gwexpy-studio-ubuntu24
if ! env_list="$(conda env list)"; then
  echo 'conda env list failed; stop' >&2
  exit 1
fi
if printf '%s\n' "$env_list" | awk -v name="$env_name" '$1 == name { found=1 } END { exit found ? 0 : 1 }'; then
  echo "conda environment already exists: $env_name" >&2
  exit 1
fi
conda create -n "$env_name" python=3.12 pip
conda activate "$env_name"
export PYTHONNOUSERSITE=1
unset PYTHONPATH
python - <<'PYTHON'
import platform
import sys

assert sys.version_info[:2] == (3, 12)
assert platform.machine() == "x86_64"
PYTHON
python --version
conda --version
python -m pip --version
```

**4. Check display libraries and install missing system packages**

No system packages are installed when the check passes. Missing packages are installed with `sudo`; ask an administrator when needed.

```bash
probe_runtime() {
  python - <<'PYTHON'
import ctypes

libraries = (
    "libEGL.so.1", "libGL.so.1", "libfontconfig.so.1", "libglib-2.0.so.0",
    "libdbus-1.so.3", "libxkbcommon.so.0", "libzstd.so.1", "libX11-xcb.so.1",
    "libxcb-cursor.so.0", "libxcb-icccm.so.4", "libxcb-image.so.0",
    "libxcb-keysyms.so.1", "libxcb-randr.so.0", "libxcb-render-util.so.0",
    "libxcb-shape.so.0", "libxcb-shm.so.0", "libxcb-sync.so.1",
    "libxcb-xfixes.so.0", "libxcb-xkb.so.1", "libxkbcommon-x11.so.0",
    "libSM.so.6", "libICE.so.6", "libwayland-client.so.0",
    "libwayland-cursor.so.0",
)
missing = []
for library in libraries:
    try:
        ctypes.CDLL(library)
    except OSError:
        missing.append(library)
if missing:
    print("Missing runtime libraries:", " ".join(missing))
    raise SystemExit(1)
print("Qt display runtime: OK")
PYTHON
}

if probe_runtime; then
  :
else
  packages=(
    libegl1 libgl1 libfontconfig1 libglib2.0-0t64 libdbus-1-3 libxkbcommon0
    libzstd1 libx11-xcb1 libxcb-cursor0 libxcb-icccm4 libxcb-image0
    libxcb-keysyms1 libxcb-randr0 libxcb-render-util0 libxcb-shape0
    libxcb-shm0 libxcb-sync1 libxcb-xfixes0 libxcb-xkb1 libxkbcommon-x11-0
    libsm6 libice6 libwayland-client0 libwayland-cursor0
  )
  unavailable=()
  sudo apt-get update
  for package in "${packages[@]}"; do
    if ! policy="$(LC_ALL=C apt-cache policy "$package")"; then
      echo "apt-cache policy failed for $package" >&2
      exit 1
    fi
    candidate="$(printf '%s\n' "$policy" | awk '/Candidate:/ { print $2; exit }')"
    if test -z "$candidate" || test "$candidate" = '(none)'; then
      unavailable+=("$package")
    fi
  done
  if ((${#unavailable[@]})); then
    printf 'No package candidate: %s\n' "${unavailable[*]}" >&2
    echo 'Ask an administrator to enable the official Ubuntu Universe repository; this guide does not change repositories.' >&2
    exit 1
  fi
  sudo apt-get install --no-install-recommends -y "${packages[@]}"
  probe_runtime
fi
```

Continue only after `Qt display runtime: OK`. Stop if libraries are still missing.

**5. Install the bundled wheel**

```bash
set -euo pipefail
shopt -s nullglob
constraint="$PWD/constraints-ubuntu24-x86_64.txt"
wheels=(./gwexpy_studio-*.whl)
test -f "$constraint"
test "${#wheels[@]}" -eq 1
wheel="${wheels[0]}"
python -m pip install --only-binary=:all: -c "$constraint" "$wheel"
```

**Launch**

```bash
gwexpy-studio
```

When Welcome appears, try **Try Sample → Load → Crop → ASD**. Continue with the save-and-reopen sequence in “Basic workflow” below.

**Launching again**

```bash
conda activate gwexpy-studio-ubuntu24
export PYTHONNOUSERSITE=1
unset PYTHONPATH
gwexpy-studio
```

[Existing Quick Start (Japanese)](docs/trial/ubuntu/Quick-Start.ja.md)

</details>

<a name="install-debian"></a>

### Debian 13 x86_64

<details>
<summary>Expand installation commands on this page</summary>

Use a Bash terminal on the Debian desktop, with conda, `unzip`, and `sha256sum`. Do not substitute a WSL2 or other Linux bundle.

**1. Verify and extract the ZIP**

```bash
set -euo pipefail
shopt -s nullglob
archives=(gwexpy-studio-trial-debian13-x86_64-*.zip)
sidecars=(gwexpy-studio-trial-debian13-x86_64-*.zip.sha256)
test "${#archives[@]}" -eq 1
test "${#sidecars[@]}" -eq 1
test "${sidecars[0]%.sha256}" = "${archives[0]}"
sha256sum -c "${sidecars[0]}"
archive="${archives[0]}"
unzip "$archive"
bundle="${archive%.zip}"
cd "$bundle"
sha256sum -c SHA256SUMS
```

Confirm that every checksum reports `OK`. Continue in the same terminal, now inside the extracted folder.

**2. Check the OS, CPU, and desktop**

```bash
set -euo pipefail
if grep -qi microsoft /proc/sys/kernel/osrelease; then
  echo "WSL is not supported by this guide." >&2
  exit 1
fi
. /etc/os-release
test "${ID:-}" = debian
test "${VERSION_ID:-}" = 13
test "$(uname -m)" = x86_64
test -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}"
printf '%s\n' "$PRETTY_NAME" "$(uname -m)"
```

**3. Create a dedicated Python 3.12 conda environment**

The procedure stops rather than overwriting an existing environment. For an existing installation, use “Launching again” below.

```bash
set -euo pipefail
env_name=gwexpy-studio-debian13
if ! env_list="$(conda env list)"; then
  echo "conda env list failed; stop." >&2
  exit 1
fi
if printf '%s\n' "$env_list" | awk -v name="$env_name" \
  '$1 == name { found=1 } END { exit found ? 0 : 1 }'; then
  echo "conda environment already exists: $env_name" >&2
  exit 1
fi
conda create -n "$env_name" python=3.12 pip
conda activate "$env_name"
export PYTHONNOUSERSITE=1
unset PYTHONPATH
python - <<'PYTHON'
import platform
import sys

assert sys.version_info[:2] == (3, 12)
assert platform.machine() == "x86_64"
PYTHON
python --version
conda --version
python -m pip --version
```

**4. Check display libraries and install missing system packages**

No system packages are installed when the check passes. Missing packages are installed with `sudo`; ask an administrator when needed.

```bash
probe_runtime() {
  python - <<'PYTHON'
import ctypes

libraries = (
    "libEGL.so.1", "libGL.so.1", "libfontconfig.so.1", "libglib-2.0.so.0",
    "libdbus-1.so.3", "libxkbcommon.so.0", "libzstd.so.1", "libX11-xcb.so.1",
    "libxcb-cursor.so.0", "libxcb-icccm.so.4", "libxcb-image.so.0",
    "libxcb-keysyms.so.1", "libxcb-randr.so.0", "libxcb-render-util.so.0",
    "libxcb-shape.so.0", "libxcb-shm.so.0", "libxcb-sync.so.1",
    "libxcb-xfixes.so.0", "libxcb-xkb.so.1", "libxkbcommon-x11.so.0",
    "libSM.so.6", "libICE.so.6", "libwayland-client.so.0",
    "libwayland-cursor.so.0",
)
missing = []
for library in libraries:
    try:
        ctypes.CDLL(library)
    except OSError:
        missing.append(library)
if missing:
    print("Missing runtime libraries:", " ".join(missing))
    raise SystemExit(1)
print("Qt display runtime: OK")
PYTHON
}

if probe_runtime; then
  :
else
  packages=(
    libegl1 libgl1 libfontconfig1 libglib2.0-0t64 libdbus-1-3
    libxkbcommon0 libzstd1 libx11-xcb1 libxcb-cursor0 libxcb-icccm4
    libxcb-keysyms1 libxcb-randr0 libxcb-shape0 libxcb-sync1
    libxcb-xfixes0 libxkbcommon-x11-0 libsm6 libwayland-cursor0
  )
  sudo apt-get update
  sudo apt-get install --no-install-recommends -y "${packages[@]}"
  probe_runtime
fi
```

Continue only after `Qt display runtime: OK`. Stop if libraries are still missing.

**5. Install the bundled wheel**

```bash
set -euo pipefail
shopt -s nullglob
wheels=(gwexpy_studio-*.whl)
test "${#wheels[@]}" -eq 1
test -f constraints-debian13-x86_64.txt
python -m pip install --only-binary=:all: \
  -c constraints-debian13-x86_64.txt \
  "./${wheels[0]}"
```

**Launch**

```bash
gwexpy-studio
```

When Welcome appears, try **Try Sample → Load → Crop → ASD**. Continue with the save-and-reopen sequence in “Basic workflow” below.

**Launching again**

```bash
conda activate gwexpy-studio-debian13
export PYTHONNOUSERSITE=1
unset PYTHONPATH
gwexpy-studio
```

[Existing Quick Start (Japanese)](docs/trial/debian/Quick-Start.ja.md)

</details>

<a name="install-wsl2"></a>

### Windows 11 / WSL2 Ubuntu 24.04

<details>
<summary>Expand installation commands on this page</summary>

Run these commands **in a WSL2 Ubuntu Bash terminal, not Windows PowerShell**. You need conda, `unzip`, `sha256sum`, and WSLg. Windows and WSL2 must use matching CPU families. The same procedure selects the constraints for x86_64 or ARM64.

**1. Verify and extract the ZIP**

```bash
set -euo pipefail
shopt -s nullglob
archives=(gwexpy-studio-trial-wsl2-ubuntu24-*.zip)
sidecars=(gwexpy-studio-trial-wsl2-ubuntu24-*.zip.sha256)
test "${#archives[@]}" -eq 1
test "${#sidecars[@]}" -eq 1
archive="${archives[0]}"
test "${sidecars[0]%.sha256}" = "$archive"
sha256sum -c "${sidecars[0]}"
unzip "$archive"
bundle="${archive%.zip}"
cd "$bundle"
sha256sum -c SHA256SUMS
```

Confirm that every checksum reports `OK`. Continue in the same terminal, now inside the extracted folder.

**2. Check the OS, CPU, and desktop**

```bash
set -euo pipefail
grep -qi microsoft /proc/sys/kernel/osrelease
test -n "${WSL_INTEROP:-}"
test -d /mnt/wslg
test -n "${WAYLAND_DISPLAY:-}${DISPLAY:-}"
grep '^ID=ubuntu$' /etc/os-release
grep '^VERSION_ID="24.04"$' /etc/os-release
guest_architecture="$(uname -m)"
case "$guest_architecture" in x86_64|aarch64) ;; *) exit 1 ;; esac
host_values="$(powershell.exe -NoLogo -NoProfile -NonInteractive -Command \
  '[Console]::OutputEncoding=[Text.UTF8Encoding]::new(); $env:PROCESSOR_ARCHITECTURE; [Environment]::OSVersion.Version.Build; (Get-CimInstance Win32_OperatingSystem).ProductType' | tr -d '\r')"
printf '%s\n' "$host_values"
host_architecture="$(printf '%s\n' "$host_values" | sed -n '1p')"
windows_build="$(printf '%s\n' "$host_values" | sed -n '2p')"
product_type="$(printf '%s\n' "$host_values" | sed -n '3p')"
host_architecture="${host_architecture//[[:space:]]/}"
windows_build="${windows_build//[[:space:]]/}"
product_type="${product_type//[[:space:]]/}"
test -n "$host_architecture"
test -n "$windows_build"
test -n "$product_type"
case "$windows_build" in ''|*[!0-9]*) exit 1 ;; esac
test "$windows_build" -ge 22000
test "$product_type" = 1
host_architecture="${host_architecture^^}"
case "$host_architecture:$guest_architecture" in
  AMD64:x86_64|ARM64:aarch64) ;;
  *) exit 1 ;;
esac
```

**3. Create a dedicated Python 3.12 conda environment**

The procedure stops rather than overwriting an existing environment. For an existing installation, use “Launching again” below.

```bash
set -euo pipefail
env_name=gwexpy-studio-wsl2
if ! env_list="$(conda env list)"; then
  echo 'conda env list failed; stop' >&2
  exit 1
fi
if printf '%s\n' "$env_list" | awk -v name="$env_name" '$1 == name { found=1 } END { exit found ? 0 : 1 }'; then
  echo "conda environment already exists: $env_name" >&2
  exit 1
fi
conda create -n "$env_name" python=3.12 pip
conda activate "$env_name"
guest_architecture="$(uname -m)"
case "$guest_architecture" in x86_64|aarch64) ;; *) exit 1 ;; esac
export PYTHONNOUSERSITE=1
unset PYTHONPATH
GUEST_ARCHITECTURE="$guest_architecture" python - <<'PYTHON'
import os
import platform
import sys

assert sys.version_info[:2] == (3, 12)
assert platform.machine().lower() == os.environ["GUEST_ARCHITECTURE"]
PYTHON
python --version
conda --version
python -m pip --version
```

**4. Check display libraries and install missing system packages**

No system packages are installed when the check passes. Missing packages are installed with `sudo`; ask an administrator when needed.

```bash
probe_runtime() {
  python - <<'PYTHON'
import ctypes

libraries = (
    "libEGL.so.1", "libGL.so.1", "libfontconfig.so.1", "libglib-2.0.so.0",
    "libdbus-1.so.3", "libxkbcommon.so.0", "libzstd.so.1", "libX11-xcb.so.1",
    "libxcb-cursor.so.0", "libxcb-icccm.so.4", "libxcb-image.so.0",
    "libxcb-keysyms.so.1", "libxcb-randr.so.0", "libxcb-render-util.so.0",
    "libxcb-shape.so.0", "libxcb-shm.so.0", "libxcb-sync.so.1",
    "libxcb-xfixes.so.0", "libxcb-xkb.so.1", "libxkbcommon-x11.so.0",
    "libSM.so.6", "libICE.so.6", "libwayland-client.so.0",
    "libwayland-cursor.so.0",
)
missing = []
for library in libraries:
    try:
        ctypes.CDLL(library)
    except OSError:
        missing.append(library)
if missing:
    print("Missing runtime libraries:", " ".join(missing))
    raise SystemExit(1)
print("Qt display runtime: OK")
PYTHON
}

if probe_runtime; then
  :
else
  packages=(
    libegl1 libgl1 libfontconfig1 libglib2.0-0t64 libdbus-1-3 libxkbcommon0
    libzstd1 libx11-xcb1 libxcb-cursor0 libxcb-icccm4 libxcb-keysyms1
    libxcb-randr0 libxcb-shape0 libxcb-sync1 libxcb-xfixes0 libxkbcommon-x11-0
    libsm6 libwayland-cursor0
  )
  unavailable=()
  sudo apt-get update
  for package in "${packages[@]}"; do
    if ! policy="$(LC_ALL=C apt-cache policy "$package")"; then
      echo "apt-cache policy failed for $package" >&2
      exit 1
    fi
    candidate="$(printf '%s\n' "$policy" | awk '/Candidate:/ { print $2; exit }')"
    if test -z "$candidate" || test "$candidate" = '(none)'; then
      unavailable+=("$package")
    fi
  done
  if ((${#unavailable[@]})); then
    printf 'No package candidate: %s\n' "${unavailable[*]}" >&2
    echo 'Ask an administrator to enable the official Ubuntu Universe repository; this guide does not change repositories.' >&2
    exit 1
  fi
  sudo apt-get install --no-install-recommends -y "${packages[@]}"
  probe_runtime
fi
```

Continue only after `Qt display runtime: OK`. Stop if libraries are still missing.

**5. Install the bundled wheel**

```bash
architecture="$(uname -m)"
constraints="constraints-ubuntu24-${architecture}.txt"
test -f "$constraints"
shopt -s nullglob
wheels=(gwexpy_studio-*.whl)
test "${#wheels[@]}" -eq 1
test -f "${wheels[0]}"
python -m pip install --only-binary=:all: -c "$constraints" "${wheels[0]}"
```

**Launch**

```bash
gwexpy-studio
```

When Welcome appears, try **Try Sample → Load → Crop → ASD**. Continue with the save-and-reopen sequence in “Basic workflow” below.

**Launching again**

```bash
conda activate gwexpy-studio-wsl2
export PYTHONNOUSERSITE=1
unset PYTHONPATH
gwexpy-studio
```

[Existing Quick Start (Japanese)](docs/trial/wsl2/Quick-Start.ja.md)

</details>

<a name="install-macos"></a>

### Apple Silicon / macOS 15+

<details>
<summary>Expand installation commands on this page</summary>

Open Terminal on your Mac. You need Apple Silicon conda, `unzip`, and `shasum`. Run the first block in your conda-enabled shell (usually zsh), then run the remaining blocks in the Bash shell it starts. This is not for Intel Macs.

**1. Start Bash with your conda configuration**

```zsh
if ! CONDA_BASE="$(conda info --base)"; then
  echo "conda info --base failed; stop before starting bash" >&2
  exit 1
fi
export CONDA_BASE
export CONDA_EXE="$CONDA_BASE/bin/conda"
export CONDA_SH="$CONDA_BASE/etc/profile.d/conda.sh"
if ! test -x "$CONDA_EXE"; then
  echo "conda executable was not found; stop before starting bash" >&2
  exit 1
fi
if ! test -r "$CONDA_SH"; then
  echo "conda initialization script was not found; stop before starting bash" >&2
  exit 1
fi
/bin/bash --noprofile --norc
```

**2. In Bash, check the OS, CPU, and ZIP, then extract**

```bash
set -e
if ! source "$CONDA_SH"; then
  echo "conda initialization failed" >&2
  exit 1
fi
set -u
set -o pipefail
shopt -s nullglob

test "$(uname -m)" = "arm64"
macos_major="$(sw_vers -productVersion | cut -d. -f1)"
test "$macos_major" -ge 15
sw_vers

zips=(gwexpy-studio-trial-macos15-arm64-*.zip)
sidecars=(gwexpy-studio-trial-macos15-arm64-*.zip.sha256)
test "${#zips[@]}" -eq 1
test "${#sidecars[@]}" -eq 1
archive="${zips[0]}"
sidecar="${sidecars[0]}"
test "${sidecar%.sha256}" = "$archive"
shasum -a 256 -c "$sidecar"
unzip "$archive"
bundle="${archive%.zip}"
test -d "$bundle"
cd "$bundle"
shasum -a 256 -c SHA256SUMS
```

Confirm that every checksum reports `OK`. Continue in the same terminal, now inside the extracted folder.

**3. Create the dedicated conda environment and install the bundled wheel**

The procedure stops rather than overwriting an existing environment. For an existing installation, use “Launching again” below.

```bash
set -euo pipefail
env_name=gwexpy-studio-macos
if ! env_list="$(conda env list)"; then
  echo "conda env list failed; stop" >&2
  exit 1
fi
if printf '%s\n' "$env_list" | awk -v name="$env_name" '$1 == name { found=1 } END { exit found ? 0 : 1 }'; then
  echo "conda environment already exists: $env_name; do not overwrite it" >&2
  exit 2
fi

conda create -n "$env_name" python=3.12 pip
conda activate "$env_name"
export PYTHONNOUSERSITE=1
unset PYTHONPATH
python - <<'PYTHON'
import platform
import sys

assert sys.version_info[:2] == (3, 12)
assert platform.machine() == "arm64"
PYTHON
python --version
conda --version
python -m pip --version

constraints_path=constraints-macos15-arm64.txt
wheel=(gwexpy_studio-*.whl)
test -f "$constraints_path"
test "${#wheel[@]}" -eq 1
wheel_path="${wheel[0]}"
test -f "$wheel_path"
python -m pip install --only-binary=:all: \
  -c "$constraints_path" \
  "$wheel_path"
```

**Launch**

```bash
gwexpy-studio
```

When Welcome appears, try **Try Sample → Load → Crop → ASD**. Continue with the save-and-reopen sequence in “Basic workflow” below.

**Launching again**

In a new terminal, first repeat the first Mac block to start Bash with your conda configuration. Do not repeat environment creation or installation.

```bash
if ! source "$CONDA_SH"; then exit 1; fi
if ! conda activate gwexpy-studio-macos; then exit 1; fi
export PYTHONNOUSERSITE=1
unset PYTHONPATH
gwexpy-studio
```

[Existing Quick Start (Japanese)](docs/trial/macos/Quick-Start.ja.md)

</details>

### If installation stops

Record the failed step and terminal message. Do not continue after a checksum or environment check fails. Reporting instructions are provided in the bundle’s `Feedback.ja.md`.

### Older Ubuntu reference release

[September 9, 2026 Ubuntu reference release](https://github.com/tatsuki-washimi/gwexpy-studio/releases/tag/trial-0.1.0a1-p.6570427-r6-a1) is separate from the new four-platform trial above. Use only the instructions bundled with that older release.

## Basic workflow

The trial workflow is deliberately small:

1. Launch Studio.
2. Select **Try Sample**.
3. Crop the time series.
4. Run **ASD**.
5. Save the project, close Studio, and reopen it.

Projects use the `.gwxproj` extension.

They record source references, operations, active state, view state, and
scientific Undo/Redo position; source data and computed arrays stay outside the
project file.

## I/O availability

Registered GWexpy formats are not automatically available in a trial build.

The UI will present each data type, format, and direction as one of:

- **Verified**: reviewed policy and a worker-process runtime probe both pass.
- **Experimental**: the runtime probe passes, with a stated caveat.
- **Unavailable**: the policy, registry, or runtime dependency does not allow it.

The effective capability snapshot is included in path-free diagnostics.

## Roadmap and source development

[ROADMAP.md](ROADMAP.md) defines the public-source, Ubuntu reference, WSL2,
macOS, and cross-platform human-trial milestones.

The source is [MIT licensed](LICENSE).

Contributors who intentionally want a development environment should read
[docs/development.md](docs/development.md).

The public trial contract is in
[docs/release/0.1.0a1-trial-readiness.md](docs/release/0.1.0a1-trial-readiness.md).

Feedback instructions are included in each bundle.
