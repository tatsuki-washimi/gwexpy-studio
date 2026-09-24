# GWexpy Studio Ubuntu trial quick start

This guide is for the `ubuntu24-x86_64` prerelease on a native Ubuntu 24.04
x86_64 computer with a normal graphical desktop. It is not for WSL2, Ubuntu on
Windows, ARM64, or another Linux distribution.

Use conda to keep the application separate from the system Python.
Git, source-code operations, and a GitHub account are not needed.

## Prerequisites

Have Bash, conda, network access for conda and pip, `unzip`, and `sha256sum`.
Use a normal graphical desktop session.
If the runtime check fails, ask an administrator to install the Ubuntu packages
listed below.

## Download and verify

Download the Ubuntu ZIP and its matching `.zip.sha256` sidecar from the release
link into an empty directory. The commands below stop unless there is exactly
one candidate of each kind. They verify both the outer archive and the files
inside it.

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

Every checksum must report `OK`. Stop without installing if any check fails.

## Confirm the host

Run this before installing anything. It rejects WSL and checks the Ubuntu
version, CPU, and graphical-session requirements.

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

If a check fails, record the output and stop. Do not treat a WSL guest as native
Ubuntu, and do not continue without a graphical desktop session.

## Create an isolated conda environment

```bash
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
python - <<'PY'
import platform
import sys

assert sys.version_info[:2] == (3, 12)
assert platform.machine() == "x86_64"
PY
python --version
conda --version
python -m pip --version
```

If this environment name already exists, stop without changing it. In a new
terminal, activate this environment and repeat the two environment settings.

## Check Qt/OpenGL prerequisites and install

The runtime check covers both native Linux display backends, X11 and Wayland.
It loads the required shared libraries and reports all missing libraries.

```bash
probe_runtime() {
  python - <<'PY'
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
PY
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

If libraries are still missing after installation, stop and send the displayed
message to the person who provided this guide.

Select the one bundled wheel and the Ubuntu constraints file explicitly.
Do not replace the constraints or build from source.

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

## Launch and use the sample workflow

```bash
gwexpy-studio
```

From Welcome, run **Try Sample → Load → Crop → ASD → Save → Close → Open**.
Open About and record the displayed Build ID. If Review or Restore appears,
record whether the workflow can continue from it.

If launch, project reopen, or recovery cannot continue, record the error and the
operation immediately before it, then contact the person who provided this
guide. Before sending it, hide usernames, hostnames, personal save locations,
credentials, and confidential measurement data.

This release supports reading `TimeSeries` data from CSV. Other file formats
and I/O operations may be unavailable. Follow the message shown by the
application.

## Report feedback

Complete `Feedback.ja.md` and reply to the person or channel that supplied the
release link. A GitHub account is not required.
