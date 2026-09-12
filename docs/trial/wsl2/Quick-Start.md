# GWexpy Studio WSL2 trial quick start

This guide is for the `wsl2-ubuntu24` prerelease on Windows 11 with Ubuntu 24.04
on WSL2 and WSLg. It runs as a Linux GUI in WSL2, not as a native Windows
product. The bundle covers x86_64 and ARM64 Windows systems with matching
x86_64 or aarch64 WSL2 systems. Do not use it on native Ubuntu or macOS.

Git, source operations, and a GitHub account are not needed. Make conda
available in the WSL2 Ubuntu terminal, with network access for conda and pip,
Bash, `unzip`, and `sha256sum`. WSLg must be available, and an administrator
must be available to install the runtime if the GL check reports missing files.
Do not modify the OS Python or another existing Python environment.

If this procedure sends an error or screen, always redact usernames, hostnames,
personal storage locations, credentials, and confidential measurement data.
Tell the guide contact what was displayed and what action immediately preceded it.

## Download and verify

Download one ZIP and its matching outer checksum file (`.zip.sha256`) from
the supplied prerelease link into an empty directory.

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

Every checksum must report `OK`. If a check fails, do not install; send the
displayed output and the preceding action to the guide contact.

## Confirm Windows 11, WSL2, WSLg, and CPU

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

Use only `AMD64` with `x86_64`, or `ARM64` with `aarch64`. Do not infer the CPU
family from a `64-bit` value. If any check fails,
redact usernames, hostnames, personal storage locations, credentials, and
confidential measurement data, then send the displayed output and preceding
action to the guide contact and stop.

## Create a dedicated conda environment

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
GUEST_ARCHITECTURE="$guest_architecture" python - <<'PY'
import os
import platform
import sys

assert sys.version_info[:2] == (3, 12)
assert platform.machine().lower() == os.environ["GUEST_ARCHITECTURE"]
PY
python --version
conda --version
python -m pip --version
```

Keep this environment active. Do not use the user site or `PYTHONPATH`.

## Check the Qt display runtime and install

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
    print("Missing display runtime libraries:", " ".join(missing))
    raise SystemExit(1)
print("Qt display runtime: OK")
PY
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
architecture="$(uname -m)"
constraints="constraints-ubuntu24-${architecture}.txt"
test -f "$constraints"
shopt -s nullglob
wheels=(gwexpy_studio-*.whl)
test "${#wheels[@]}" -eq 1
test -f "${wheels[0]}"
python -m pip install --only-binary=:all: -c "$constraints" "${wheels[0]}"
```

If the matching constraints or exactly one wheel is not found, or installation
fails, do not change files or build from source. Send the displayed output and
the preceding action to the guide contact.

## Run the workflow

```bash
gwexpy-studio
```

When Welcome appears, run **Try Sample → Load → Crop → ASD → Save → Close →
Open**. If Review or Restore appears, record that it appeared and whether you
could continue. Record the Build ID shown in About.

If launch, project reopen, or recovery cannot continue, do not try a workaround.
Send the screen, terminal output, and preceding action to the guide contact.

Complete `Feedback.ja.md` and reply to the supplied email or chat. A GitHub
account is not required. Do not send confidential measurement data, credentials,
usernames, hostnames, device-specific paths, or unrelated logs.

## Known I/O limitation

The covered I/O is reading a TimeSeries CSV. Other I/O may be shown as
unavailable and may not run. If the display or operation differs from the
expected behavior, send the screen and preceding action to the guide contact.
