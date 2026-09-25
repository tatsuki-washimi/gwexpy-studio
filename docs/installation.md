# Installation

This page takes you from installation to the first Studio workflow.
You do not need to move between platform pages during the normal setup.

## 1. Choose a route

| Goal | Route | You need |
| --- | --- | --- |
| Use a validated prerelease | ZIP from the matching GitHub Release | Matching ZIP, checksum, and conda |
| Run or modify the source | python -m pip install . | Source checkout and Python 3.12 |

The trial route and the source-checkout route are different.
The trial route uses a prebuilt wheel, pinned constraints, and platform qualification.
The source route resolves the package dependencies for development or local evaluation.

## 2. Install the trial build

### Download the two Release assets

From the [GWexpy Studio GitHub Releases](https://github.com/tatsuki-washimi/gwexpy-studio/releases) page, download these two files with the same Build ID into an empty directory:

~~~text
gwexpy-studio-trial-<target>-<build-id>.zip
gwexpy-studio-trial-<target>-<build-id>.zip.sha256
~~~

Verify the outer checksum, unpack the archive, and then verify the checksums inside the bundle.

On Ubuntu or WSL2:

~~~bash
set -euo pipefail
shopt -s nullglob
sidecars=(gwexpy-studio-trial-*.zip.sha256)
test "${#sidecars[@]}" -eq 1
sha256sum -c "${sidecars[0]}"
archive="${sidecars[0]%.sha256}"
unzip "$archive"
bundle="${archive%.zip}"
cd "$bundle"
sha256sum -c SHA256SUMS
~~~

For Debian, use the target-specific checksum commands in the Debian section below.
On macOS, replace sha256sum with shasum -a 256.
If either checksum fails, stop without installing.

### Check the host and install

Open only the section that matches the host.
A failed preflight is a stop condition, not a reason to switch to a source install or different constraints.

<details open>
<summary>Windows 11 WSL2 / Ubuntu 24.04 / WSLg</summary>

Use this route only when the same Release contains a wsl2-ubuntu24 asset.
Confirm whether the guest uses x86_64 or aarch64 and use the matching constraints file.

~~~bash
set -euo pipefail
grep -qi microsoft /proc/sys/kernel/osrelease
test -d /mnt/wslg
grep '^ID=ubuntu$' /etc/os-release
grep '^VERSION_ID="24.04"$' /etc/os-release
architecture="$(uname -m)"
case "$architecture" in x86_64|aarch64) ;; *) exit 1 ;; esac
powershell.exe -NoLogo -NoProfile -NonInteractive -Command \
  '$env:PROCESSOR_ARCHITECTURE; [Environment]::OSVersion.Version'
~~~

Stop here if /mnt/wslg, powershell.exe, Ubuntu 24.04, or a matching architecture cannot be confirmed.

~~~bash
conda create -n gwexpy-studio-wsl2 python=3.12
conda activate gwexpy-studio-wsl2
export PYTHONNOUSERSITE=1
unset PYTHONPATH

python - <<'PY'
import ctypes

for library in ("libEGL.so.1", "libGL.so.1"):
    ctypes.CDLL(library)
print("Qt GL runtime: OK")
PY

architecture="$(uname -m)"
pip install --only-binary=:all: \
  -c "constraints-ubuntu24-${architecture}.txt" \
  ./gwexpy_studio-*.whl
gwexpy-studio
~~~

If the Qt GL check fails, install the Ubuntu runtime libraries with administrator access and run the check again:

~~~bash
sudo apt-get update
sudo apt-get install --no-install-recommends -y libegl1 libgl1
~~~

</details>

<details>
<summary>Native Ubuntu 24.04</summary>

Use this route only when the matching Ubuntu asset is included in the same Release.

~~~bash
set -euo pipefail
grep '^ID=ubuntu$' /etc/os-release
grep '^VERSION_ID="24.04"$' /etc/os-release
architecture="$(uname -m)"
case "$architecture" in x86_64|aarch64) ;; *) exit 1 ;; esac
~~~

~~~bash
conda create -n gwexpy-studio python=3.12
conda activate gwexpy-studio
export PYTHONNOUSERSITE=1
unset PYTHONPATH

python - <<'PY'
import ctypes

for library in ("libEGL.so.1", "libGL.so.1"):
    ctypes.CDLL(library)
print("Qt GL runtime: OK")
PY

architecture="$(uname -m)"
pip install --only-binary=:all: \
  -c "constraints-ubuntu24-${architecture}.txt" \
  ./gwexpy_studio-*.whl
gwexpy-studio
~~~

If libEGL.so.1 or libGL.so.1 is missing, install libegl1 and libgl1 before retrying.

</details>

<details>
<summary>Native Debian 13 / x86_64</summary>

Use this route only when the same Release contains the `debian13-x86_64` asset.
Use a native Debian 13 x86_64 graphical desktop, Bash, conda, `unzip`, and `sha256sum`.
Conda and pip need network access. An administrator may need to install the listed Qt display packages.

From the empty download directory containing the Debian ZIP and its `.zip.sha256` sidecar, verify and unpack exactly one matching pair:

~~~bash
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
~~~

Every checksum must report `OK`. Stop if any check fails.
In a terminal in the graphical desktop session, confirm the host:

~~~bash
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
~~~

Stop if the host check fails. Create a dedicated conda environment, refusing to overwrite an existing one:

~~~bash
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
~~~

Keep this environment active. In each new trial terminal, activate it again, set `PYTHONNOUSERSITE=1`, and unset `PYTHONPATH`.
Check the Qt display libraries; install only the documented Debian packages if the check fails, then run it again:

~~~bash
set -euo pipefail
probe_runtime() {
  python - <<'PY'
import ctypes

libraries = (
    "libEGL.so.1", "libGL.so.1", "libfontconfig.so.1", "libglib-2.0.so.0",
    "libdbus-1.so.3", "libxkbcommon.so.0", "libzstd.so.1", "libX11-xcb.so.1",
    "libxcb-cursor.so.0", "libxcb-icccm.so.4", "libxcb-image.so.0",
    "libxcb-randr.so.0", "libxcb-render-util.so.0",
    "libxcb-shape.so.0", "libxcb-shm.so.0", "libxcb-sync.so.1",
    "libxcb-xfixes.so.0", "libxcb-xkb.so.1", "libxkbcommon-x11.so.0",
    "libSM.so.6", "libICE.so.6", "libwayland-client.so.0",
    "libwayland-cursor.so.0", "libxcb-keysyms.so.1",
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
~~~

Install the one bundled wheel with its Debian constraints, then launch Studio:

~~~bash
set -euo pipefail
shopt -s nullglob
wheels=(gwexpy_studio-*.whl)
test "${#wheels[@]}" -eq 1
test -f constraints-debian13-x86_64.txt
python -m pip install --only-binary=:all: \
  -c constraints-debian13-x86_64.txt \
  "./${wheels[0]}"
gwexpy-studio
~~~

If another library is missing or installation fails, record the displayed error and the step that failed. The [Debian Quick Start](trial/debian/Quick-Start.md) has more diagnostic detail.

</details>

<details>
<summary>macOS 15 or newer / Apple Silicon</summary>

Use this route only when the same Release contains a macos15-arm64 asset.
Intel Macs are not supported by this route.

~~~bash
set -euo pipefail
test "$(uname -m)" = "arm64"
test "$(sw_vers -productVersion | cut -d. -f1)" -ge 15
sw_vers
~~~

~~~bash
conda create -n gwexpy-studio-macos python=3.12
conda activate gwexpy-studio-macos
export PYTHONNOUSERSITE=1
unset PYTHONPATH
pip install --only-binary=:all: \
  -c constraints-macos15-arm64.txt \
  ./gwexpy_studio-*.whl
gwexpy-studio
~~~

</details>

## 3. Install from a source checkout

Use this route when trial qualification is not required.
The supported Python version is 3.12.

~~~bash
python3.12 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/python -m pip check
.venv/bin/gwexpy-studio
~~~

python -m pip install . installs the runtime dependencies, including PySide6, and creates the gwexpy-studio launcher inside .venv.
Pass one .gwxproj path to the launcher to open a saved project.

To add test and lint tools, install the dev extra in the same environment:

~~~bash
.venv/bin/python -m pip install '.[dev]'
~~~

When actively editing the checkout, an editable install is also available:

~~~bash
.venv/bin/python -m pip install -e '.[dev]'
~~~

Editable installation is for development and is not the trial-participant route.

## 4. The four steps after launch

The following images show the workflow shared by the trial and source routes.
You can follow these steps on this page without moving to a platform-specific page.

### Press Try Sample on the Welcome screen

![GWexpy Studio Welcome screen after startup](images/installation/01-welcome.png)

After startup preparation completes, **Try Sample** becomes available on the Welcome screen.
Choose **Open Data** instead when using your own data.

### Load the sample

![TimeSeries loaded through Try Sample](images/installation/02-try-sample.png)

Review the sample and press **Load** to display the TimeSeries in the central view.
The Sources panel shows the selected object, and Metadata shows its basic properties.

### Crop the TimeSeries and run ASD

![Crop and ASD result](images/installation/03-asd.png)

With the TimeSeries selected, run **Operations → Crop**, then run **Operations → ASD**.
History records timeseries.crop and timeseries.asd.

### Save and reopen the project

![Saved project reopened in GWexpy Studio](images/installation/04-reopen-project.png)

Use **File → Save** to save a .gwxproj, close Studio, and launch it again.
If **Review / Restore Data** is required after reopening, follow the displayed review step.
The project stores source references, operations, active state, view state, and the Undo/Redo position.

## 5. Read supported trial data

The trial reads source files through these routes and does not write back to them:

| Data type | Format and reader |
| --- | --- |
| `TimeSeries` | CSV |
| `TimeSeries`, `TimeSeriesDict` | GWF: `gwf.lalframe` (recommended); `gwf` (also selectable) |
| `TimeSeries`, `TimeSeriesDict`, `TimeSeriesMatrix` | DiagGUI XML: `xml.diaggui`, product `TS` |
| `TimeSeries`, `TimeSeriesDict` | NDScope HDF5: `hdf.ndscope` |

In **Open Data**, choose the data type and format, press **Inspect / Review**, review the result, and then explicitly press **Read Data**.
Dropping a file only fills the generic **Open Data** form; it does not identify the data type or format, or start a read.

On GWexpy 0.2.0, DiagGUI XML reads into `FrequencySeries`, `FrequencySeriesDict`, and `FrequencySeriesMatrix` are currently unavailable and fail closed.

## 6. Minimum checks

Run these commands in the environment that should launch Studio:

~~~bash
python -m pip check
python -c "import gwexpy, PySide6; print('runtime imports: OK')"
test -x "$(command -v gwexpy-studio)" && echo "launcher: OK"
~~~

For a trial build, also confirm the Build ID in the Release asset and its bundled manifest.
Do not replace the trial wheel install with python -m pip install ., change its constraints, or build a dependency from source.

## If Studio does not start

First save the output from pip check, the architecture check, the Python version check, and the Qt GL check.
On WSL2, confirm /mnt/wslg and powershell.exe again.

Use the following only when you need detailed qualification records, diagnostics, or feedback formats:

- [Ubuntu Quick Start](trial/ubuntu/Quick-Start.md)
- [Debian Quick Start](trial/debian/Quick-Start.md)
- [WSL2 Quick Start](trial/wsl2/Quick-Start.md)
- [macOS Quick Start](trial/macos/Quick-Start.md)

If a WSL2 preflight fails, do not continue from native Ubuntu or use a source build as a workaround.
The source route does not carry a trial Build ID, release checksum, qualification record, or pinned trial resolution.
