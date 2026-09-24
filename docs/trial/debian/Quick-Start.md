# GWexpy Studio Debian 13 trial quick start

This guide applies only to the `debian13-x86_64` prerelease on a native Debian
13 x86_64 system with a graphical desktop. Do not use it on WSL, ARM, another
Debian release, or another Linux distribution.

Git and source-code operations are not needed, and a GitHub account is not
needed. Install conda before starting. Both conda and pip need network access.
Use a dedicated conda environment with Python 3.12. Do not change the Debian
system Python.

## Prerequisites

Before downloading the bundle, have these available:

- Bash, conda, `unzip`, and `sha256sum`;
- network access for both conda and pip;
- a Debian 13 x86_64 graphical desktop session;
- permission to ask an administrator to install the documented GL packages if
  the GL check reports that they are missing.

If the program reports that another library is missing, tell the sender the
exact message.

## Download and verify

Download the ZIP and its matching `.zip.sha256` sidecar from the single
prerelease link into an empty directory. The checks below require exactly one
candidate of each kind and verify that the sidecar names that ZIP.

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

Every checksum must report `OK`. If a check fails, do not install anything;
stop this guide. Redact personal information before telling the sender what was
displayed and which command failed.

## Confirm Debian, x86_64, native Linux, and a graphical desktop

Run the following in a terminal belonging to the graphical desktop session.
The first check rejects a WSL kernel before any installation step.

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

If a check fails, do not install anything; stop this guide. Redact personal
information before telling the sender what was displayed and which command
failed.

## Create an isolated environment

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

If the environment already exists, or if its Python version or CPU does not
match, stop without installing the wheel. Keep the environment active. In
every new terminal used for the trial, activate it again, set
`PYTHONNOUSERSITE=1`, and unset `PYTHONPATH`.

## Check Qt GL and install

The runtime check loads the shared libraries required by the Qt display
backends. If any are missing, update the Debian package lists, install the
documented packages, and run the same check again.

```bash
set -euo pipefail
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

Install the wheel with the target constraints. The following discovery check
requires exactly one wheel in the bundle and passes that exact path to pip.

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

If installation or launch fails, tell the sender what was displayed and which
step failed.

## Run the workflow

```bash
gwexpy-studio
```

From the Welcome screen, complete **Try Sample → Load → Crop → ASD → Save →
Close → Open**. Use the application file dialogs for Save and Open. Open About
and record its Build ID.

If launch, project reopen, or recovery cannot continue, tell the sender what
was displayed and which action was performed immediately before the failure.

This distribution checks reading a time-series CSV. Operations that are not
available are shown on screen.

Complete `Feedback.ja.md` and reply to the email or chat that supplied the
prerelease link. Before sending, redact usernames, hostnames, personal save
locations, credentials, and confidential measurement data; tell the sender what
the error displayed and which action immediately preceded it.
